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
"""The switch-user filter bridges the principal it switches to into ``RequestContext`` (WP14 follow-up).

It set only ``request.state.security_context``: method security and the CQRS query cache, which read
``RequestContext.security_context``, still saw the administrator during the switch request, and the original
principal during the exit request.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.session.session import HttpSession
from pyfly.web.adapters.starlette.filters.switch_user_filter import SwitchUserFilter

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


@pytest.fixture
def request_context() -> Iterator[RequestContext]:
    context = RequestContext.init()
    try:
        yield context
    finally:
        RequestContext.clear()


def _request(path: str, query: str, session: HttpSession) -> Request:
    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": query.encode()}
    request = Request(scope)
    request.state.session = session
    return request


async def _call_next(_request: Request) -> Response:
    return PlainTextResponse("downstream")


@pytest.mark.asyncio
async def test_switch_and_exit_update_the_request_context(request_context: RequestContext) -> None:
    admin = SecurityContext(user_id="admin", roles=["ADMIN"])
    request_context.security_context = admin
    session = HttpSession("sid", {_SECURITY_CONTEXT_KEY: admin})
    switch = SwitchUserFilter(InMemoryUserDetailsService(UserDetails(username="bob", password_hash="x")))

    await switch.do_filter(_request("/login/impersonate", "username=bob", session), _call_next)

    impersonated = request_context.security_context
    assert impersonated is not None and impersonated.user_id == "bob"

    await switch.do_filter(_request("/logout/impersonate", "", session), _call_next)

    assert request_context.security_context is admin
