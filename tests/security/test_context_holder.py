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
"""SecurityContextHolder: one answer to "who is doing this?" (C075).

The session-restoring filter set only ``request.state.security_context``, while the auditor and method
security read ``RequestContext``: a session-authenticated user was nobody to them. The holder reads the
context the security filters establish for the request, live, and a context set on the holder (``run_as``)
wins over both.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.context.request_context import RequestContext
from pyfly.security import SecurityContextHolder as ExportedHolder
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import REQUEST_STATE_ATTRIBUTE, SecurityContextHolder
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.filters.request_context_filter import RequestContextFilter


def _request() -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    return Request(scope, receive)


def test_exported_from_the_security_package() -> None:
    assert ExportedHolder is SecurityContextHolder


def test_nothing_established_means_no_context() -> None:
    assert SecurityContextHolder.get_context() is None
    assert SecurityContextHolder.get_authenticated_user_id() is None


def test_a_context_set_on_the_holder_wins_and_is_restored() -> None:
    request_context = RequestContext.init()
    request_context.security_context = SecurityContext(user_id="from-request-context")
    try:
        with SecurityContextHolder.using(SecurityContext(user_id="job")):
            assert SecurityContextHolder.get_authenticated_user_id() == "job"
            token = SecurityContextHolder.set_context(SecurityContext(user_id="inner"))
            assert SecurityContextHolder.get_authenticated_user_id() == "inner"
            SecurityContextHolder.reset_context(token)
            assert SecurityContextHolder.get_authenticated_user_id() == "job"
        assert SecurityContextHolder.get_authenticated_user_id() == "from-request-context"
    finally:
        RequestContext.clear()


def test_the_request_state_is_read_live_and_wins_over_request_context() -> None:
    request = _request()
    request_context = RequestContext.init()
    request_context.set(REQUEST_STATE_ATTRIBUTE, request.state)
    request_context.security_context = SecurityContext(user_id="bearer-subject")
    try:
        assert SecurityContextHolder.get_authenticated_user_id() == "bearer-subject"
        request.state.security_context = SecurityContext(user_id="session-user")  # a later filter
        assert SecurityContextHolder.get_authenticated_user_id() == "session-user"
        request_context.security_context = None
        request.state.security_context = SecurityContext.anonymous()
        assert SecurityContextHolder.get_context() == SecurityContext.anonymous()
        assert SecurityContextHolder.get_authenticated_user_id() is None
    finally:
        RequestContext.clear()


def test_an_authenticated_request_context_wins_over_an_anonymous_request_state() -> None:
    """``SecurityFilter`` always sets ``request.state.security_context``, the anonymous context when it
    authenticated nobody; a principal a custom filter or the application put on ``RequestContext`` (the
    older bridge) is still the one seen then."""
    request = _request()
    request_context = RequestContext.init()
    request_context.set(REQUEST_STATE_ATTRIBUTE, request.state)
    try:
        request.state.security_context = SecurityContext.anonymous()
        request_context.security_context = SecurityContext(user_id="custom-filter-user")
        assert SecurityContextHolder.get_authenticated_user_id() == "custom-filter-user"
        request_context.security_context = SecurityContext.anonymous()
        assert SecurityContextHolder.get_context() == SecurityContext.anonymous()
    finally:
        RequestContext.clear()


async def test_a_task_started_in_the_block_inherits_the_context() -> None:
    async def who() -> str | None:
        return SecurityContextHolder.get_authenticated_user_id()

    with SecurityContextHolder.using(SecurityContext(user_id="parent")):
        task = asyncio.create_task(who())
    assert await task == "parent"
    assert SecurityContextHolder.get_context() is None


async def test_request_context_filter_exposes_the_request_state() -> None:
    seen: list[str | None] = []

    async def downstream(request: Request) -> Response:
        request.state.security_context = SecurityContext(user_id="established-later")
        seen.append(SecurityContextHolder.get_authenticated_user_id())
        return PlainTextResponse("ok")

    await RequestContextFilter().do_filter(_request(), downstream)

    assert seen == ["established-later"]
    assert RequestContext.current() is None


async def test_session_filter_restores_the_principal_into_request_context_too() -> None:
    stored = SecurityContext(user_id="alice", roles=["ADMIN"])
    request = _request()
    request.state.session = HttpSession("sid", {"SECURITY_CONTEXT": stored})
    RequestContext.init()
    seen: list[Any] = []

    async def downstream(request: Request) -> Response:
        request_context = RequestContext.current()
        assert request_context is not None
        seen.append(request_context.security_context)
        return PlainTextResponse("ok")

    try:
        await OAuth2SessionSecurityFilter().do_filter(request, downstream)
    finally:
        RequestContext.clear()

    assert seen == [stored]
    assert request.state.security_context == stored
