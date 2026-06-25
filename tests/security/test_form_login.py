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
"""Form-login filter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.security.authentication import DaoAuthenticationProvider, ProviderManager
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.filters.form_login_filter import FormLoginFilter

_ENCODER = BcryptPasswordEncoder(rounds=4)
_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


def _manager() -> ProviderManager:
    service = InMemoryUserDetailsService(
        UserDetails(username="alice", password_hash=_ENCODER.hash("pw"), roles=["ADMIN"])
    )
    return ProviderManager(DaoAuthenticationProvider(service, _ENCODER))


def _post(path: str, data: dict[str, str]) -> Request:
    body = urlencode(data).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    request = Request(scope, receive)
    request.state.session = HttpSession("pre-auth-sid", {})
    return request


async def _call_next(request: Request) -> Response:
    return PlainTextResponse("downstream")


class TestFormLoginFilter:
    @pytest.mark.asyncio
    async def test_valid_login_establishes_session_context(self) -> None:
        flt = FormLoginFilter(_manager())
        request = _post("/login", {"username": "alice", "password": "pw"})
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        ctx = request.state.session.get_attribute(_SECURITY_CONTEXT_KEY)
        assert ctx is not None and ctx.user_id == "alice" and ctx.has_role("ADMIN")

    @pytest.mark.asyncio
    async def test_session_id_is_rotated_on_login(self) -> None:
        flt = FormLoginFilter(_manager())
        request = _post("/login", {"username": "alice", "password": "pw"})
        await flt.do_filter(request, _call_next)
        assert request.state.session.id != "pre-auth-sid"  # fixation defense

    @pytest.mark.asyncio
    async def test_invalid_login_redirects_to_failure(self) -> None:
        flt = FormLoginFilter(_manager())
        request = _post("/login", {"username": "alice", "password": "WRONG"})
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 302
        assert "error" in resp.headers["location"]
        assert request.state.session.get_attribute(_SECURITY_CONTEXT_KEY) is None

    @pytest.mark.asyncio
    async def test_non_login_request_passes_through(self) -> None:
        flt = FormLoginFilter(_manager())
        request = _post("/other", {"x": "y"})
        resp = await flt.do_filter(request, _call_next)
        assert resp.body == b"downstream"

    @pytest.mark.asyncio
    async def test_json_mode_returns_200_and_401(self) -> None:
        flt = FormLoginFilter(_manager(), use_redirect=False)
        ok = await flt.do_filter(_post("/login", {"username": "alice", "password": "pw"}), _call_next)
        assert ok.status_code == 200
        bad = await flt.do_filter(_post("/login", {"username": "alice", "password": "no"}), _call_next)
        assert bad.status_code == 401


class TestFormLoginAndLogoutAutoConfigEndToEnd:
    """Form-login and logout auto-configs wire their filters into the live chain."""

    def _client(self) -> Any:
        import contextlib
        from collections.abc import AsyncIterator

        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.starlette.app import create_app

        config = Config(
            {
                "pyfly": {
                    "security": {
                        "csrf": {"enabled": "false"},
                        "form-login": {
                            "enabled": "true",
                            "use-redirect": "false",
                            "users": {"alice": {"password-hash": _ENCODER.hash("pw"), "roles": "ADMIN"}},
                        },
                        "logout": {"enabled": "true", "use-redirect": "false"},
                    }
                }
            }
        )
        ctx = ApplicationContext(config)

        @contextlib.asynccontextmanager
        async def _lifespan(_app: Any) -> AsyncIterator[None]:
            await ctx.start()
            yield
            await ctx.stop()

        return TestClient(create_app(context=ctx, lifespan=_lifespan))

    def test_form_login_endpoint_authenticates(self) -> None:
        with self._client() as client:
            ok = client.post("/login", data={"username": "alice", "password": "pw"})
            assert ok.status_code == 200 and ok.json()["authenticated"] is True
            bad = client.post("/login", data={"username": "alice", "password": "WRONG"})
            assert bad.status_code == 401

    def test_logout_endpoint_wired(self) -> None:
        with self._client() as client:
            resp = client.post("/logout")
            assert resp.status_code == 204
