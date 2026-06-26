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
"""Sender-constrained tokens — DPoP (RFC 9449) + mTLS (RFC 8705)."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import time

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.dpop import (
    DPoPProofValidator,
    certificate_thumbprint,
    confirm_dpop_binding,
    confirm_mtls_binding,
    jwk_thumbprint,
)


def _ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _public_jwk(key: ec.EllipticCurvePrivateKey) -> dict:
    return json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))


def _proof(key: ec.EllipticCurvePrivateKey, *, htm: str, htu: str, iat: int | None = None, jti: str = "id1") -> str:
    claims = {"htm": htm, "htu": htu, "iat": iat if iat is not None else int(time.time()), "jti": jti}
    return pyjwt.encode(claims, key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": _public_jwk(key)})


class TestJwkThumbprint:
    def test_thumbprint_is_stable_and_base64url(self) -> None:
        key = _ec_key()
        jwk = _public_jwk(key)
        t1 = jwk_thumbprint(jwk)
        t2 = jwk_thumbprint(dict(reversed(list(jwk.items()))))  # member order must not matter
        assert t1 == t2
        assert "=" not in t1 and "+" not in t1 and "/" not in t1


class TestDPoPProofValidator:
    def test_valid_proof_returns_jkt(self) -> None:
        key = _ec_key()
        proof = _proof(key, htm="GET", htu="https://api.example.com/resource")
        jkt = DPoPProofValidator().validate(proof, http_method="GET", http_url="https://api.example.com/resource")
        assert jkt == jwk_thumbprint(_public_jwk(key))

    def test_htu_query_is_ignored(self) -> None:
        key = _ec_key()
        proof = _proof(key, htm="GET", htu="https://api.example.com/resource")
        # The request URL may carry a query string; htu compares origin+path only.
        jkt = DPoPProofValidator().validate(proof, http_method="GET", http_url="https://api.example.com/resource?a=1")
        assert jkt

    def test_method_mismatch_rejected(self) -> None:
        key = _ec_key()
        proof = _proof(key, htm="GET", htu="https://api.example.com/x")
        with pytest.raises(SecurityException):
            DPoPProofValidator().validate(proof, http_method="POST", http_url="https://api.example.com/x")

    def test_url_mismatch_rejected(self) -> None:
        key = _ec_key()
        proof = _proof(key, htm="GET", htu="https://api.example.com/x")
        with pytest.raises(SecurityException):
            DPoPProofValidator().validate(proof, http_method="GET", http_url="https://api.example.com/y")

    def test_stale_proof_rejected(self) -> None:
        key = _ec_key()
        proof = _proof(key, htm="GET", htu="https://api.example.com/x", iat=int(time.time()) - 600)
        with pytest.raises(SecurityException):
            DPoPProofValidator(max_age_seconds=60).validate(
                proof, http_method="GET", http_url="https://api.example.com/x"
            )

    def test_replay_rejected(self) -> None:
        key = _ec_key()
        validator = DPoPProofValidator(replay_cache=set())
        proof = _proof(key, htm="GET", htu="https://api.example.com/x", jti="unique-1")
        validator.validate(proof, http_method="GET", http_url="https://api.example.com/x")
        with pytest.raises(SecurityException):
            validator.validate(proof, http_method="GET", http_url="https://api.example.com/x")

    def test_symmetric_alg_rejected(self) -> None:
        # A proof must be signed with an asymmetric key; alg=none/HS* is rejected.
        forged = pyjwt.encode(
            {"htm": "GET", "htu": "https://api/x", "iat": int(time.time()), "jti": "j"},
            "secret",
            algorithm="HS256",
            headers={"typ": "dpop+jwt", "jwk": {"kty": "oct"}},
        )
        with pytest.raises(SecurityException):
            DPoPProofValidator().validate(forged, http_method="GET", http_url="https://api/x")


class TestDPoPBindingConfirmation:
    def test_matching_jkt_passes(self) -> None:
        confirm_dpop_binding({"cnf": {"jkt": "abc"}}, "abc")

    def test_mismatched_jkt_raises(self) -> None:
        with pytest.raises(SecurityException):
            confirm_dpop_binding({"cnf": {"jkt": "abc"}}, "different")

    def test_missing_cnf_raises(self) -> None:
        with pytest.raises(SecurityException):
            confirm_dpop_binding({"sub": "u"}, "abc")


def _self_signed_cert() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "client")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


class TestResourceFilterDPoPEnforcement:
    """The resource-server filter enforces proof-of-possession for cnf-bound tokens."""

    def _filter_and_request(self, jkt: str, *, dpop_header: str | None):
        from starlette.requests import Request

        from pyfly.security.context import SecurityContext
        from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import (
            ERROR_MODE_401,
            OAuth2ResourceServerFilter,
        )

        class _FakeValidator:
            def validate_and_context(self, token: str) -> tuple[dict, SecurityContext]:
                return {"sub": "u", "cnf": {"jkt": jkt}}, SecurityContext(user_id="u")

        headers: list[tuple[bytes, bytes]] = [(b"authorization", b"DPoP the-access-token")]
        if dpop_header is not None:
            headers.append((b"dpop", dpop_header.encode("latin-1")))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/r",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("api.example.com", 443),
        }
        flt = OAuth2ResourceServerFilter(
            _FakeValidator(),  # type: ignore[arg-type]
            error_mode=ERROR_MODE_401,
            enforce_sender_constraints=True,
        )
        return flt, Request(scope)

    @pytest.mark.asyncio
    async def test_valid_dpop_proof_accepted(self) -> None:
        key = _ec_key()
        jkt = jwk_thumbprint(_public_jwk(key))
        # ath must match the access token the filter passes ("the-access-token").
        from pyfly.security.oauth2.dpop import access_token_hash

        claims = {
            "htm": "GET",
            "htu": "https://api.example.com/r",
            "iat": int(time.time()),
            "jti": "p1",
            "ath": access_token_hash("the-access-token"),
        }
        proof = pyjwt.encode(claims, key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": _public_jwk(key)})
        flt, request = self._filter_and_request(jkt, dpop_header=proof)

        captured = {}

        async def call_next(r):
            captured["ctx"] = r.state.security_context
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("ok")

        resp = await flt.do_filter(request, call_next)
        assert resp.status_code == 200
        assert captured["ctx"].user_id == "u"

    @pytest.mark.asyncio
    async def test_missing_dpop_proof_rejected(self) -> None:
        key = _ec_key()
        jkt = jwk_thumbprint(_public_jwk(key))
        flt, request = self._filter_and_request(jkt, dpop_header=None)

        async def call_next(r):
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("should not reach")

        resp = await flt.do_filter(request, call_next)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_proof_rejected(self) -> None:
        bound_key = _ec_key()
        jkt = jwk_thumbprint(_public_jwk(bound_key))
        # Attacker presents a proof signed with a DIFFERENT key.
        attacker = _ec_key()
        from pyfly.security.oauth2.dpop import access_token_hash

        claims = {
            "htm": "GET",
            "htu": "https://api.example.com/r",
            "iat": int(time.time()),
            "jti": "p2",
            "ath": access_token_hash("the-access-token"),
        }
        proof = pyjwt.encode(
            claims, attacker, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": _public_jwk(attacker)}
        )
        flt, request = self._filter_and_request(jkt, dpop_header=proof)

        async def call_next(r):
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("should not reach")

        resp = await flt.do_filter(request, call_next)
        assert resp.status_code == 401


class TestMtlsBinding:
    def test_thumbprint_matches_manual_sha256(self) -> None:
        pem = _self_signed_cert()
        cert = x509.load_pem_x509_certificate(pem)
        expected = base64.urlsafe_b64encode(hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest())
        assert certificate_thumbprint(pem) == expected.rstrip(b"=").decode("ascii")

    def test_confirm_matching_cert(self) -> None:
        pem = _self_signed_cert()
        thumb = certificate_thumbprint(pem)
        confirm_mtls_binding({"cnf": {"x5t#S256": thumb}}, pem)

    def test_confirm_mismatched_cert_raises(self) -> None:
        pem = _self_signed_cert()
        other = _self_signed_cert()
        thumb = certificate_thumbprint(other)
        with pytest.raises(SecurityException):
            confirm_mtls_binding({"cnf": {"x5t#S256": thumb}}, pem)
