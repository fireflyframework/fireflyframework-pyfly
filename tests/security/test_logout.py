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
