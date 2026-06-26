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
"""Generic logout filter (Spring ``logout`` / ``LogoutConfigurer``).

Handles a POST to the logout URL by invalidating the HTTP session, clearing the
security context, and deleting configured cookies — independent of OAuth2. Browser
(redirect) and API (204) responses are both supported.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


class LogoutFilter(OncePerRequestFilter):
    """Invalidates the session on a POST to the logout URL.

    Runs at ``HIGHEST_PRECEDENCE + 235`` (after form login). Configure the URL,
    success URL, response mode, and cookies to clear.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 235

    def __init__(
        self,
        *,
        logout_url: str = "/logout",
        logout_success_url: str = "/login?logout",
        delete_cookies: Sequence[str] = (),
        use_redirect: bool = True,
    ) -> None:
        self._logout_url = logout_url
        self._logout_success_url = logout_success_url
        self._delete_cookies = list(delete_cookies)
        self._use_redirect = use_redirect

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        if request.method == "POST" and request.url.path == self._logout_url:
            return self._logout(request)
        return await call_next(request)  # type: ignore[no-any-return]

    def _logout(self, request: Request) -> Response:
        session = getattr(getattr(request, "state", None), "session", None)
        if session is not None:
            session.set_attribute(_SECURITY_CONTEXT_KEY, None)
            session.invalidate()
        if hasattr(request, "state"):
            from pyfly.security.context import SecurityContext

            request.state.security_context = SecurityContext.anonymous()
        response: Response
        if self._use_redirect:
            response = RedirectResponse(url=self._logout_success_url, status_code=302)
        else:
            response = Response(status_code=204)
        for cookie in self._delete_cookies:
            response.delete_cookie(cookie, path="/")
        logger.info("Logout processed for path %s", request.url.path)
        return response
