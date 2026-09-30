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

import asyncio
import ipaddress
import json
import math
import threading
import time
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import jwt

from pyfly.security.oauth2.jwks import AsyncJWKSFetcher, JWKSFetcher


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


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("JWKS operation deadline exceeded")
    return remaining


def _fetch_bytes(uri: str, *, timeout: float, max_bytes: int) -> bytes:
    validate_endpoint(uri)
    validate_limit(timeout, "timeout")
    validate_limit(max_bytes, "max_bytes", integer=True)
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(uri, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    with opener.open(request, timeout=remaining_seconds(deadline)) as response:
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
    return bytes(body)


def _parse_document(body: bytes, max_bytes: int) -> dict[str, Any]:
    if not isinstance(body, bytes) or len(body) > max_bytes:
        raise ValueError("JWKS fetcher must return bytes within the configured limit")
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("Document must be a JSON object")
    return document


def fetch_json(uri: str, *, timeout: float, max_bytes: int) -> dict[str, Any]:
    return _parse_document(_fetch_bytes(uri, timeout=timeout, max_bytes=max_bytes), max_bytes)


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
        fetcher: JWKSFetcher | None = None,
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
        self._fetcher = fetcher
        self._timeout = timeout
        self._lifespan = lifespan
        self._min_refresh_seconds = min_refresh_seconds
        self._max_bytes = max_bytes
        self._max_keys = max_keys
        self._keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        self._expires_at = 0.0
        self._next_refresh_at = 0.0
        self._lock = threading.Lock()

    def _fetch_keys(self, deadline: float) -> dict[str, tuple[jwt.PyJWK, str | None]]:
        if self._fetcher is None:
            document = fetch_json(self._uri, timeout=remaining_seconds(deadline), max_bytes=self._max_bytes)
        else:
            body = self._fetcher(self._uri, timeout=remaining_seconds(deadline), max_bytes=self._max_bytes)
            remaining_seconds(deadline)
            document = _parse_document(body, self._max_bytes)
        return self._parse_keys(document, deadline)

    def _parse_keys(self, document: dict[str, Any], deadline: float) -> dict[str, tuple[jwt.PyJWK, str | None]]:
        remaining_seconds(deadline)
        entries = document.get("keys")
        if not isinstance(entries, list) or not entries or len(entries) > self._max_keys:
            raise ValueError("JWKS must contain a nonempty, bounded keys array")
        keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        for entry in entries:
            remaining_seconds(deadline)
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
        remaining_seconds(deadline)
        if not keys:
            raise ValueError("JWKS contains no usable signing keys")
        return keys

    def get_signing_key(self, kid: str, algorithm: str, *, deadline: float | None = None) -> jwt.PyJWK:
        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=min(remaining_seconds(deadline), threading.TIMEOUT_MAX)):
            raise jwt.PyJWKClientError("JWKS operation deadline exceeded")
        try:
            if deadline is not None:
                remaining_seconds(deadline)
            cached = self._cached_key(kid, algorithm)
            if cached is not None:
                return cached
            now = time.monotonic()
            refresh_deadline = now + self._timeout
            if deadline is not None:
                refresh_deadline = min(refresh_deadline, deadline)
            try:
                keys = self._fetch_keys(refresh_deadline)
                remaining_seconds(refresh_deadline)
            except Exception:
                # The borrowed callback may raise arbitrary exceptions containing
                # credentials, URLs or response bytes. Keep them out of the chain.
                raise jwt.PyJWKClientError("Unable to refresh JWKS") from None
            finally:
                self._next_refresh_at = time.monotonic() + self._min_refresh_seconds
            return self._publish_keys(keys, kid, algorithm)
        finally:
            self._lock.release()

    def _cached_key(self, kid: str, algorithm: str) -> jwt.PyJWK | None:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._matching_key(kid, algorithm)
        if now < self._next_refresh_at:
            raise jwt.PyJWKClientError("JWKS refresh is rate limited")
        return None

    def _publish_keys(self, keys: dict[str, tuple[jwt.PyJWK, str | None]], kid: str, algorithm: str) -> jwt.PyJWK:
        initial = not self._keys and self._expires_at == 0
        self._keys = keys
        self._expires_at = time.monotonic() + self._lifespan
        # One immediate rotation after a successful cold load; subsequent
        # refresh attempts share the cooldown, including failures.
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


@asynccontextmanager
async def _refresh_timeout(delay: float) -> AsyncIterator[None]:
    # A regular asyncio.timeout would cancel again if external cancellation is
    # already running the fetcher's cleanup when the refresh deadline arrives.
    task = asyncio.current_task()
    assert task is not None
    cancellations = task.cancelling()
    expired = False

    def expire() -> None:
        nonlocal expired
        if task.cancelling() == cancellations:
            expired = True
            task.cancel()

    handle = asyncio.get_running_loop().call_later(delay, expire)
    try:
        yield
    except asyncio.CancelledError:
        if expired and task.cancelling() == cancellations + 1:
            raise TimeoutError("JWKS operation deadline exceeded") from None
        raise
    finally:
        handle.cancel()
        if expired:
            task.uncancel()


class AsyncBoundedJWKSClient(BoundedJWKSClient):
    """Independent snapshot and single-flight lock for one event loop."""

    def __init__(
        self,
        uri: str,
        *,
        timeout: float,
        lifespan: float,
        min_refresh_seconds: float,
        max_bytes: int,
        max_keys: int,
        async_fetcher: AsyncJWKSFetcher,
    ) -> None:
        super().__init__(
            uri,
            timeout=timeout,
            lifespan=lifespan,
            min_refresh_seconds=min_refresh_seconds,
            max_bytes=max_bytes,
            max_keys=max_keys,
        )
        self._async_fetcher = async_fetcher
        self._async_lock = asyncio.Lock()

    async def get_signing_key_async(self, kid: str, algorithm: str, *, deadline: float | None = None) -> jwt.PyJWK:
        if deadline is None:
            await self._async_lock.acquire()
        else:
            async with asyncio.timeout(remaining_seconds(deadline)):
                await self._async_lock.acquire()
        try:
            if deadline is not None:
                remaining_seconds(deadline)
            cached = self._cached_key(kid, algorithm)
            if cached is not None:
                return cached
            refresh_deadline = time.monotonic() + self._timeout
            if deadline is not None:
                refresh_deadline = min(refresh_deadline, deadline)
            try:
                async with _refresh_timeout(remaining_seconds(refresh_deadline)):
                    body = await self._async_fetcher(
                        self._uri, timeout=remaining_seconds(refresh_deadline), max_bytes=self._max_bytes
                    )
                    remaining_seconds(refresh_deadline)
                    keys = self._parse_keys(_parse_document(body, self._max_bytes), refresh_deadline)
                    remaining_seconds(refresh_deadline)
            except Exception:
                raise jwt.PyJWKClientError("Unable to refresh JWKS") from None
            finally:
                self._next_refresh_at = time.monotonic() + self._min_refresh_seconds
            return self._publish_keys(keys, kid, algorithm)
        finally:
            self._async_lock.release()
