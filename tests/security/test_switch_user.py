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
"""switch-user (run-as impersonation) filter."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.security.context import SecurityContext
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.filters.switch_user_filter import (
    PREVIOUS_PRINCIPAL_ROLE,
    SwitchUserFilter,
)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


def _uds() -> InMemoryUserDetailsService:
    return InMemoryUserDetailsService(
        UserDetails(username="bob", password_hash="x", roles=["USER"]),
        UserDetails(username="carol", password_hash="x", roles=["USER"], enabled=False),
    )


def _request(path: str, query: str = "", *, current: SecurityContext | None = None) -> Request:
    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": query.encode()}
    request = Request(scope)
    session = HttpSession("sid", {})
    if current is not None:
        session.set_attribute(_SECURITY_CONTEXT_KEY, current)
    request.state.session = session
    return request


async def _call_next(request: Request) -> Response:
    return PlainTextResponse("downstream")


def _admin() -> SecurityContext:
    return SecurityContext(user_id="admin", roles=["ADMIN"])


class TestSwitchUserFilter:
    @pytest.mark.asyncio
    async def test_admin_can_impersonate(self) -> None:
        flt = SwitchUserFilter(_uds())
        request = _request("/login/impersonate", "username=bob", current=_admin())
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 302
        ctx = request.state.session.get_attribute(_SECURITY_CONTEXT_KEY)
        assert ctx.user_id == "bob"
        assert ctx.has_role(PREVIOUS_PRINCIPAL_ROLE)  # marker for "currently impersonating"

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self) -> None:
        flt = SwitchUserFilter(_uds())
        request = _request("/login/impersonate", "username=bob", current=SecurityContext(user_id="joe", roles=["USER"]))
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self) -> None:
        flt = SwitchUserFilter(_uds())
        resp = await flt.do_filter(_request("/login/impersonate", "username=bob"), _call_next)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_target_not_found(self) -> None:
        flt = SwitchUserFilter(_uds())
        resp = await flt.do_filter(_request("/login/impersonate", "username=ghost", current=_admin()), _call_next)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_target_not_found(self) -> None:
        flt = SwitchUserFilter(_uds())
        resp = await flt.do_filter(_request("/login/impersonate", "username=carol", current=_admin()), _call_next)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_exit_restores_original_principal(self) -> None:
        flt = SwitchUserFilter(_uds())
        # First impersonate.
        request = _request("/login/impersonate", "username=bob", current=_admin())
        await flt.do_filter(request, _call_next)
        session = request.state.session

        # Then exit on the same session.
        exit_req = Request(
            {"type": "http", "method": "GET", "path": "/logout/impersonate", "headers": [], "query_string": b""}
        )
        exit_req.state.session = session
        resp = await flt.do_filter(exit_req, _call_next)
        assert resp.status_code == 302
        restored = session.get_attribute(_SECURITY_CONTEXT_KEY)
        assert restored.user_id == "admin" and not restored.has_role(PREVIOUS_PRINCIPAL_ROLE)

    @pytest.mark.asyncio
    async def test_non_switch_path_passes_through(self) -> None:
        flt = SwitchUserFilter(_uds())
        resp = await flt.do_filter(_request("/other", current=_admin()), _call_next)
        assert resp.body == b"downstream"
