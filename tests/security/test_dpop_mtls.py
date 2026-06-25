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
        jkt = DPoPProofValidator().validate(
            proof, http_method="GET", http_url="https://api.example.com/resource?a=1"
        )
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
