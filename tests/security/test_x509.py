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
"""X.509 client-certificate authentication filter."""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.web.adapters.starlette.filters.x509_filter import X509AuthenticationFilter


def _cert(cn: str) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _request(cert_pem: str | None, header: str = "x-client-cert") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cert_pem is not None:
        headers.append((header.encode(), cert_pem.encode("latin-1")))
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": headers, "query_string": b""}
    return Request(scope)


async def _call_next(request: Request) -> Response:
    return PlainTextResponse("ok")


class TestX509Filter:
    @pytest.mark.asyncio
    async def test_cert_without_user_service_authenticates_principal(self) -> None:
        flt = X509AuthenticationFilter()
        request = _request(_cert("alice"))
        await flt.do_filter(request, _call_next)
        ctx = request.state.security_context
        assert ctx.is_authenticated and ctx.user_id == "alice"

    @pytest.mark.asyncio
    async def test_cert_with_user_service_loads_authorities(self) -> None:
        uds = InMemoryUserDetailsService(UserDetails(username="alice", password_hash="x", roles=["ADMIN"]))
        flt = X509AuthenticationFilter(user_details_service=uds)
        request = _request(_cert("alice"))
        await flt.do_filter(request, _call_next)
        assert request.state.security_context.has_role("ADMIN")

    @pytest.mark.asyncio
    async def test_unknown_user_401_mode_rejected(self) -> None:
        uds = InMemoryUserDetailsService()
        flt = X509AuthenticationFilter(user_details_service=uds, error_mode="401")
        resp = await flt.do_filter(_request(_cert("ghost")), _call_next)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_cert_is_anonymous(self) -> None:
        flt = X509AuthenticationFilter(error_mode="401")
        request = _request(None)
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 200
        assert not request.state.security_context.is_authenticated

    @pytest.mark.asyncio
    async def test_malformed_cert_401_mode_rejected(self) -> None:
        flt = X509AuthenticationFilter(error_mode="401")
        resp = await flt.do_filter(_request("not-a-cert"), _call_next)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_user_rejected(self) -> None:
        uds = InMemoryUserDetailsService(UserDetails(username="bob", password_hash="x", enabled=False))
        flt = X509AuthenticationFilter(user_details_service=uds, error_mode="401")
        resp = await flt.do_filter(_request(_cert("bob")), _call_next)
        assert resp.status_code == 401
