# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Bounded HTTP documents and expiring, single-flight JWKS snapshots."""

from __future__ import annotations

import ipaddress
import json
import math
import threading
import time
import urllib.request
from http.client import HTTPException
from typing import Any
from urllib.parse import urlsplit

import jwt


def validate_endpoint(uri: str) -> None:
    """Require TLS, with HTTP allowed only for local development endpoints."""
    parsed = urlsplit(uri)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Endpoint must be an absolute URL without credentials or a fragment")
    # Accessing port also validates malformed/out-of-range port values.
    _ = parsed.port
    if parsed.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise ValueError("Endpoint requires HTTPS (HTTP is allowed only on loopback)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Trust applies to the configured endpoint, not to an arbitrary Location.
        return None


def validate_limit(value: float, name: str, *, integer: bool = False, allow_zero: bool = False) -> None:
    valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
    if integer:
        valid_type = valid_type and isinstance(value, int)
    try:
        valid = valid_type and math.isfinite(value) and (value >= 0 if allow_zero else value > 0)
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"Invalid numeric limit: {name}")


def fetch_json(uri: str, *, timeout: float, max_bytes: int) -> dict[str, Any]:
    validate_endpoint(uri)
    validate_limit(timeout, "timeout")
    validate_limit(max_bytes, "max_bytes", integer=True)
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(uri, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + timeout
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Endpoint did not return HTTP 200")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("Compressed documents are not supported")
        body = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Document fetch deadline exceeded")
            # read1 performs one underlying read, so a drip-fed response cannot
            # reset the application deadline with every arriving byte.
            chunk = response.read1(min(16384, max_bytes + 1 - len(body)))
            if time.monotonic() >= deadline:
                raise TimeoutError("Document fetch deadline exceeded")
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("Document exceeds configured byte limit")
            if not chunk:
                break
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("Document must be a JSON object")
    return document


class BoundedJWKSClient:
    """One bounded snapshot; no per-token or negative-key cache.

    The lock serializes refreshes and rechecks freshness for waiting callers.
    Failed refreshes leave the previous snapshot and its original expiry intact.
    """

    def __init__(
        self,
        uri: str,
        *,
        timeout: float,
        lifespan: float,
        min_refresh_seconds: float,
        max_bytes: int,
        max_keys: int,
    ) -> None:
        validate_endpoint(uri)
        for name, value in (
            ("jwks_timeout", timeout),
            ("jwks_cache_seconds", lifespan),
            ("jwks_min_refresh_seconds", min_refresh_seconds),
        ):
            validate_limit(value, name)
        validate_limit(max_bytes, "jwks_max_bytes", integer=True)
        validate_limit(max_keys, "jwks_max_keys", integer=True)
        self._uri = uri
        self._timeout = timeout
        self._lifespan = lifespan
        self._min_refresh_seconds = min_refresh_seconds
        self._max_bytes = max_bytes
        self._max_keys = max_keys
        self._keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        self._expires_at = 0.0
        self._next_refresh_at = 0.0
        self._lock = threading.Lock()

    def _fetch_keys(self) -> dict[str, tuple[jwt.PyJWK, str | None]]:
        document = fetch_json(self._uri, timeout=self._timeout, max_bytes=self._max_bytes)
        entries = document.get("keys")
        if not isinstance(entries, list) or not entries or len(entries) > self._max_keys:
            raise ValueError("JWKS must contain a nonempty, bounded keys array")
        keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid JWK entry")
            if entry.get("use", "sig") != "sig" or "verify" not in entry.get("key_ops", ["verify"]):
                continue
            if entry.get("kty") not in ("RSA", "EC", "OKP"):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            if kid in keys:
                raise ValueError("JWKS contains duplicate signing key identifiers")
            algorithm = entry.get("alg")
            if algorithm is not None and not isinstance(algorithm, str):
                raise ValueError("Invalid JWK algorithm")
            keys[kid] = (jwt.PyJWK.from_dict(entry), algorithm)
        if not keys:
            raise ValueError("JWKS contains no usable signing keys")
        return keys

    def get_signing_key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._matching_key(kid, algorithm)
            if now < self._next_refresh_at:
                raise jwt.PyJWKClientError("JWKS refresh is rate limited")
            initial = not self._keys and self._expires_at == 0
            try:
                keys = self._fetch_keys()
            except (OSError, HTTPException, ValueError, TypeError, jwt.PyJWTError, RecursionError) as exc:
                raise jwt.PyJWKClientError("Unable to refresh JWKS") from exc
            finally:
                self._next_refresh_at = time.monotonic() + self._min_refresh_seconds
            self._keys = keys
            self._expires_at = time.monotonic() + self._lifespan
            # A successful cold load can be followed by one immediate rotation
            # refresh. Every subsequent refresh (including failures) is throttled.
            if initial and kid in keys:
                self._next_refresh_at = 0.0
            return self._matching_key(kid, algorithm)

    def _matching_key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        entry = self._keys.get(kid)
        if entry is None:
            raise jwt.PyJWKClientError("No matching signing key")
        key, declared_algorithm = entry
        if declared_algorithm is not None and declared_algorithm != algorithm:
            raise jwt.PyJWKClientError("Signing key algorithm does not match token")
        return key
