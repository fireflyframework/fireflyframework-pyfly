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
"""Sender-constrained access tokens — DPoP (RFC 9449) and mTLS (RFC 8705).

A bearer token can be replayed by anyone who steals it. Sender-constraining binds
the token to a key the legitimate client holds:

* **DPoP** — the client signs a per-request *proof* JWT with its private key; the
  access token carries ``cnf.jkt`` (the JWK SHA-256 thumbprint, RFC 7638). The
  resource server verifies the proof and that its key thumbprint matches ``jkt``.
* **mTLS** — the access token carries ``cnf["x5t#S256"]`` (the client certificate
  thumbprint). The resource server compares it to the presented client cert.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

import jwt as pyjwt

from pyfly.kernel.exceptions import SecurityException


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """Compute the RFC 7638 JWK SHA-256 thumbprint (base64url, no padding)."""
    kty = jwk.get("kty")
    if kty == "RSA":
        members = {"e": jwk["e"], "kty": "RSA", "n": jwk["n"]}
    elif kty == "EC":
        members = {"crv": jwk["crv"], "kty": "EC", "x": jwk["x"], "y": jwk["y"]}
    elif kty == "OKP":
        members = {"crv": jwk["crv"], "kty": "OKP", "x": jwk["x"]}
    else:
        raise SecurityException(f"Unsupported JWK key type for thumbprint: {kty!r}", code="INVALID_TOKEN")
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


def _normalize_htu(url: str) -> str:
    """Normalize an HTTP URI for ``htu`` comparison: scheme://host/path (no query/fragment)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def access_token_hash(access_token: str) -> str:
    """The DPoP ``ath`` value: base64url(SHA-256(access_token))."""
    return _b64url(hashlib.sha256(access_token.encode("ascii")).digest())


class DPoPProofValidator:
    """Validates a DPoP proof JWT (RFC 9449 §4.3) and returns its key thumbprint.

    Args:
        max_age_seconds: Accepted ``iat`` skew window for the proof.
        replay_cache: Optional set-like collection of seen ``jti`` values; when
            provided, a repeated ``jti`` is rejected as a replay. (Use a bounded /
            TTL-backed set in production.)
    """

    def __init__(self, *, max_age_seconds: int = 60, replay_cache: set[str] | None = None) -> None:
        self._max_age = max_age_seconds
        self._replay_cache = replay_cache

    def validate(
        self,
        proof: str,
        *,
        http_method: str,
        http_url: str,
        access_token: str | None = None,
    ) -> str:
        """Verify *proof* for the given request; return the bound key thumbprint (jkt).

        Raises:
            SecurityException: if the proof is malformed, mis-signed, stale, replayed,
                or does not match the request method/URL (or access token hash).
        """
        try:
            header = pyjwt.get_unverified_header(proof)
        except pyjwt.PyJWTError as exc:
            raise SecurityException(f"Malformed DPoP proof: {exc}", code="INVALID_DPOP_PROOF") from exc

        if header.get("typ") != "dpop+jwt":
            raise SecurityException("DPoP proof has wrong 'typ'", code="INVALID_DPOP_PROOF")
        alg = str(header.get("alg", ""))
        if alg[:2] not in ("RS", "ES", "PS") and not alg.startswith("Ed"):
            raise SecurityException("DPoP proof must use an asymmetric algorithm", code="INVALID_DPOP_PROOF")
        jwk = header.get("jwk")
        if not isinstance(jwk, dict):
            raise SecurityException("DPoP proof missing embedded 'jwk'", code="INVALID_DPOP_PROOF")
        if any(k in jwk for k in ("d", "p", "q", "dp", "dq", "qi")):
            raise SecurityException("DPoP proof 'jwk' must not contain private material", code="INVALID_DPOP_PROOF")

        try:
            key = pyjwt.PyJWK.from_dict(jwk).key
            claims = pyjwt.decode(proof, key, algorithms=[alg], options={"verify_aud": False})
        except pyjwt.PyJWTError as exc:
            raise SecurityException(f"DPoP proof signature invalid: {exc}", code="INVALID_DPOP_PROOF") from exc

        if str(claims.get("htm", "")).upper() != http_method.upper():
            raise SecurityException("DPoP 'htm' does not match the request method", code="INVALID_DPOP_PROOF")
        if _normalize_htu(str(claims.get("htu", ""))) != _normalize_htu(http_url):
            raise SecurityException("DPoP 'htu' does not match the request URL", code="INVALID_DPOP_PROOF")

        iat = claims.get("iat")
        if not isinstance(iat, (int, float)) or abs(time.time() - float(iat)) > self._max_age:
            raise SecurityException("DPoP proof is stale or missing 'iat'", code="INVALID_DPOP_PROOF")

        jti = claims.get("jti")
        if self._replay_cache is not None:
            if not jti or jti in self._replay_cache:
                raise SecurityException("DPoP proof replayed or missing 'jti'", code="INVALID_DPOP_PROOF")
            self._replay_cache.add(str(jti))

        if access_token is not None and claims.get("ath") != access_token_hash(access_token):
            raise SecurityException("DPoP 'ath' does not match the access token", code="INVALID_DPOP_PROOF")

        return jwk_thumbprint(jwk)


def confirm_dpop_binding(token_claims: dict[str, Any], jkt: str) -> None:
    """Assert the access token is DPoP-bound to *jkt* (its ``cnf.jkt``)."""
    bound = (token_claims.get("cnf") or {}).get("jkt")
    if not bound:
        raise SecurityException("Access token is not DPoP-bound (no cnf.jkt)", code="INVALID_TOKEN")
    if bound != jkt:
        raise SecurityException("DPoP key does not match the token's cnf.jkt", code="INVALID_TOKEN")


def certificate_thumbprint(cert: str | bytes) -> str:
    """Return the RFC 8705 ``x5t#S256`` thumbprint (base64url SHA-256 of the DER cert)."""
    from cryptography import x509

    if isinstance(cert, str):
        cert = cert.encode("utf-8")
    loaded = x509.load_pem_x509_certificate(cert) if b"-----BEGIN" in cert else x509.load_der_x509_certificate(cert)
    from cryptography.hazmat.primitives.serialization import Encoding

    return _b64url(hashlib.sha256(loaded.public_bytes(Encoding.DER)).digest())


def confirm_mtls_binding(token_claims: dict[str, Any], cert: str | bytes) -> None:
    """Assert the access token is mTLS-bound to *cert* (its ``cnf["x5t#S256"]``)."""
    bound = (token_claims.get("cnf") or {}).get("x5t#S256")
    if not bound:
        raise SecurityException("Access token is not mTLS-bound (no cnf.x5t#S256)", code="INVALID_TOKEN")
    if bound != certificate_thumbprint(cert):
        raise SecurityException("Client certificate does not match the token's cnf.x5t#S256", code="INVALID_TOKEN")
