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
"""Generic logout filter."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.container.container import Container
from pyfly.core.config import Config
from pyfly.security.auto_configuration import LogoutAutoConfiguration
from pyfly.security.context import SecurityContext
from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.concurrency import ConcurrencyControlPolicy, InMemorySessionRegistry, SessionConcurrencyController
from pyfly.session.filter import SessionFilter
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.filters.logout_filter import LogoutFilter


def _post(path: str) -> Request:
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    request = Request(scope)
    session = HttpSession("sid", {})
    session.set_attribute("SECURITY_CONTEXT", object())
    request.state.session = session
    return request


async def _call_next(request: Request) -> Response:
    return PlainTextResponse("downstream")


class TestLogoutFilter:
    @pytest.mark.asyncio
    async def test_logout_invalidates_session_and_redirects(self) -> None:
        flt = LogoutFilter()
        request = _post("/logout")
        resp = await flt.do_filter(request, _call_next)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?logout"
        assert request.state.session.invalidated is True

    @pytest.mark.asyncio
    async def test_logout_clears_configured_cookies(self) -> None:
        flt = LogoutFilter(delete_cookies=["SESSION", "XSRF-TOKEN"])
        resp = await flt.do_filter(_post("/logout"), _call_next)
        set_cookie = (
            resp.headers.getlist("set-cookie") if hasattr(resp.headers, "getlist") else [resp.headers["set-cookie"]]
        )
        joined = " ".join(set_cookie)
        assert "SESSION=" in joined and "XSRF-TOKEN=" in joined

    @pytest.mark.asyncio
    async def test_non_logout_passes_through(self) -> None:
        flt = LogoutFilter()
        resp = await flt.do_filter(_post("/other"), _call_next)
        assert resp.body == b"downstream"

    @pytest.mark.asyncio
    async def test_json_mode_returns_204(self) -> None:
        flt = LogoutFilter(use_redirect=False)
        resp = await flt.do_filter(_post("/logout"), _call_next)
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_custom_logout_url(self) -> None:
        flt = LogoutFilter(logout_url="/sign-out")
        resp = await flt.do_filter(_post("/sign-out"), _call_next)
        assert resp.status_code == 302
        # The default path is no longer special.
        passed = await flt.do_filter(_post("/logout"), _call_next)
        assert passed.body == b"downstream"


def _logged_in_post(path: str, user_id: str, session_id: str) -> Request:
    request = Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""})
    session = HttpSession(session_id, {})
    session.set_attribute("SECURITY_CONTEXT", SecurityContext(user_id=user_id))
    request.state.session = session
    return request


def _controller() -> SessionConcurrencyController:
    return SessionConcurrencyController(InMemorySessionRegistry(), ConcurrencyControlPolicy(max_sessions=1))


class TestLogoutDeregistersTheSession:
    """The OAuth2 login handler's logout deregistered the session from the concurrency controller; this filter
    did not, so the registration of a logged-out session stayed until the controller dropped it as dead, or,
    beside a controller with no session store to check, counted toward the cap until evicted."""

    @pytest.mark.asyncio
    async def test_logout_deregisters_the_session_with_the_controller(self) -> None:
        controller = _controller()
        assert await controller.on_login("ada", "sid", 1.0)

        await LogoutFilter(concurrency=controller).do_filter(_logged_in_post("/logout", "ada", "sid"), _call_next)

        assert await controller.registry.count("ada") == 0

    @pytest.mark.asyncio
    async def test_the_auto_configured_filter_uses_the_controller_bean(self) -> None:
        controller = _controller()
        container = Container()
        container.register_instance(SessionConcurrencyController, controller)
        assert await controller.on_login("ada", "sid", 1.0)
        logout_filter = LogoutAutoConfiguration().logout_filter(Config({}), container)

        await logout_filter.do_filter(_logged_in_post("/logout", "ada", "sid"), _call_next)

        assert await controller.registry.count("ada") == 0

    @pytest.mark.asyncio
    async def test_without_a_controller_bean_the_logout_still_invalidates(self) -> None:
        logout_filter = LogoutAutoConfiguration().logout_filter(Config({}), Container())
        request = _logged_in_post("/logout", "ada", "sid")

        response = await logout_filter.do_filter(request, _call_next)

        assert response.status_code == 302 and request.state.session.invalidated

    @pytest.mark.asyncio
    async def test_a_registry_outage_does_not_undo_the_logout(self) -> None:
        """The deregistration ran first and its failure answered 500 with the session never invalidated: it stayed
        in the store, logged in. The logout now ends the session and logs the failed deregistration."""

        class _Unreachable(InMemorySessionRegistry):
            async def deregister(self, principal: str, session_id: str) -> None:
                raise ConnectionError("the session registry is unreachable")

        store = InMemorySessionStore()
        await store.save("sid", {"SECURITY_CONTEXT": SecurityContext(user_id="ada")}, ttl=60)
        controller = SessionConcurrencyController(_Unreachable(), ConcurrencyControlPolicy(max_sessions=1))
        logout_filter = LogoutFilter(concurrency=controller)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/logout",
                "headers": [(b"cookie", b"PYFLY_SESSION=sid")],
                "query_string": b"",
            }
        )

        async def through_the_logout_filter(inner: Request) -> Response:
            response: Response = await logout_filter.do_filter(inner, _call_next)
            return response

        response = await SessionFilter(store=store).do_filter(request, through_the_logout_filter)

        assert response.status_code == 302
        assert not await store.exists("sid")
