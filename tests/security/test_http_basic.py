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
"""Tests for HTTP Basic authentication (UserDetailsService + filter)."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails, UserDetailsService
from pyfly.web.adapters.starlette.filters.http_basic_filter import HttpBasicAuthenticationFilter

_ENCODER = BcryptPasswordEncoder(rounds=4)


def _service() -> InMemoryUserDetailsService:
    return InMemoryUserDetailsService(
        UserDetails(username="alice", password_hash=_ENCODER.hash("s3cret"), roles=["ADMIN"]),
        UserDetails(username="bob", password_hash=_ENCODER.hash("hunter2"), roles=["USER"], enabled=False),
    )


def _request(auth_header: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode("latin-1")))
    scope: dict[str, Any] = {"type": "http", "method": "GET", "path": "/x", "headers": headers, "query_string": b""}
    return Request(scope)


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


async def _call_next(request: Request) -> Response:
    return PlainTextResponse("ok")


class TestInMemoryUserDetailsService:
    @pytest.mark.asyncio
    async def test_loads_known_user(self) -> None:
        svc = _service()
        user = await svc.load_user_by_username("alice")
        assert user is not None and user.username == "alice"

    @pytest.mark.asyncio
    async def test_unknown_user_is_none(self) -> None:
        assert await _service().load_user_by_username("nobody") is None

    def test_protocol_conformance(self) -> None:
        assert isinstance(_service(), UserDetailsService)


class TestHttpBasicFilter:
    @pytest.mark.asyncio
    async def test_valid_credentials_set_authenticated_context(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER)
        request = _request(_basic("alice", "s3cret"))
        response = await f.do_filter(request, _call_next)
        assert response.status_code == 200
        ctx = request.state.security_context
        assert ctx.is_authenticated
        assert ctx.user_id == "alice"
        assert ctx.has_role("ADMIN")

    @pytest.mark.asyncio
    async def test_wrong_password_401_with_challenge(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401", realm="PyFly")
        response = await f.do_filter(_request(_basic("alice", "wrong")), _call_next)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="PyFly"'

    @pytest.mark.asyncio
    async def test_unknown_user_401(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401")
        response = await f.do_filter(_request(_basic("ghost", "x")), _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_user_rejected(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401")
        response = await f.do_filter(_request(_basic("bob", "hunter2")), _call_next)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_password_anonymous_mode_falls_through(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="anonymous")
        request = _request(_basic("alice", "wrong"))
        response = await f.do_filter(request, _call_next)
        assert response.status_code == 200  # gate decides downstream
        assert not request.state.security_context.is_authenticated

    @pytest.mark.asyncio
    async def test_no_header_is_anonymous(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401")
        request = _request(None)
        response = await f.do_filter(request, _call_next)
        assert response.status_code == 200  # missing creds fall through to the gate
        assert not request.state.security_context.is_authenticated

    @pytest.mark.asyncio
    async def test_non_basic_scheme_ignored(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401")
        request = _request("Bearer sometoken")
        response = await f.do_filter(request, _call_next)
        assert response.status_code == 200
        assert not request.state.security_context.is_authenticated

    @pytest.mark.asyncio
    async def test_malformed_base64_rejected(self) -> None:
        f = HttpBasicAuthenticationFilter(_service(), _ENCODER, error_mode="401")
        response = await f.do_filter(_request("Basic !!!not-base64!!!"), _call_next)
        assert response.status_code == 401


class TestHttpBasicAutoConfigEndToEnd:
    """HTTP Basic wired from config, exercised through the full app stack."""

    def _app(self) -> Any:
        import contextlib
        from collections.abc import AsyncIterator

        from pyfly.container.stereotypes import rest_controller
        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.starlette.app import create_app
        from pyfly.web.mappings import get_mapping, request_mapping

        @rest_controller
        @request_mapping("/api/secret")
        class _SecretController:
            @get_mapping("/")
            async def secret(self) -> dict:
                return {"ok": True}

        config = Config(
            {
                "pyfly": {
                    "security": {
                        "csrf": {"enabled": "false"},
                        "http-basic": {
                            "enabled": "true",
                            "realm": "PyFly",
                            "error-mode": "401",
                            "users": {"alice": {"password-hash": _ENCODER.hash("s3cret"), "roles": "ADMIN"}},
                        },
                    }
                }
            }
        )
        ctx = ApplicationContext(config)
        ctx.register_bean(_SecretController)

        @contextlib.asynccontextmanager
        async def _lifespan(_app: Any) -> AsyncIterator[None]:
            await ctx.start()
            yield
            await ctx.stop()

        return create_app(context=ctx, lifespan=_lifespan)

    def test_valid_basic_credentials_pass(self) -> None:
        from starlette.testclient import TestClient

        with TestClient(self._app()) as client:
            resp = client.get("/api/secret/", headers={"Authorization": _basic("alice", "s3cret")})
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}

    def test_bad_credentials_get_401_challenge(self) -> None:
        from starlette.testclient import TestClient

        with TestClient(self._app()) as client:
            resp = client.get("/api/secret/", headers={"Authorization": _basic("alice", "WRONG")})
            assert resp.status_code == 401
            assert resp.headers["WWW-Authenticate"] == 'Basic realm="PyFly"'
