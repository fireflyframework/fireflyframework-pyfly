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
(redirect) and API (204) responses are both supported. Given the session concurrency
controller, it also deregisters the session, as the OAuth2 login handler's logout does.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

if TYPE_CHECKING:
    from pyfly.session.concurrency import SessionConcurrencyController

logger = logging.getLogger(__name__)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


class LogoutFilter(OncePerRequestFilter):
    """Invalidates the session on a POST to the logout URL.

    Runs at ``HIGHEST_PRECEDENCE + 235`` (after form login). Configure the URL,
    success URL, response mode, and cookies to clear.

    Args:
        concurrency: An optional ``SessionConcurrencyController``: a logout deregisters the
            session from it, so the per-principal cap stops counting the session at once.
            Without it the registration stays until the controller drops it as dead (at the
            principal's next capped login, or through the purge) — and, beside a controller
            with no session store to check, counts until the session is evicted. The session
            is invalidated before it is deregistered: a deregistration that fails (the
            registry down) is logged as ``session_deregistration_failed`` and never undoes
            the logout.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 235

    def __init__(
        self,
        *,
        logout_url: str = "/logout",
        logout_success_url: str = "/login?logout",
        delete_cookies: Sequence[str] = (),
        use_redirect: bool = True,
        concurrency: SessionConcurrencyController | None = None,
    ) -> None:
        self._logout_url = logout_url
        self._logout_success_url = logout_success_url
        self._delete_cookies = list(delete_cookies)
        self._use_redirect = use_redirect
        self._concurrency = concurrency

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        if request.method == "POST" and request.url.path == self._logout_url:
            return await self._logout(request)
        return await call_next(request)  # type: ignore[no-any-return]

    async def _logout(self, request: Request) -> Response:
        session = getattr(getattr(request, "state", None), "session", None)
        if session is not None:
            user_id = getattr(session.get_attribute(_SECURITY_CONTEXT_KEY), "user_id", None)
            session.set_attribute(_SECURITY_CONTEXT_KEY, None)
            session.invalidate()
            if self._concurrency is not None and user_id is not None:
                # After the invalidation: a failed deregistration never undoes the logout.
                try:
                    await self._concurrency.on_logout(user_id, session.id)
                except Exception:  # noqa: BLE001 — the session has ended already; a stale registration is dropped later
                    logger.warning("session_deregistration_failed", exc_info=True)
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
