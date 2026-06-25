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
"""Form-login filter (Spring ``formLogin``).

Processes a POST of username/password to the login URL, authenticates via an
:class:`~pyfly.security.authentication.ProviderManager`, and on success rotates
the session id (fixation defense) and stores the :class:`SecurityContext` in the
session — where :class:`OAuth2SessionSecurityFilter` restores it on later
requests. Browser (redirect) and API (JSON) responses are both supported.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.security.authentication import Authentication, AuthenticationException, ProviderManager
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


class FormLoginFilter(OncePerRequestFilter):
    """Authenticates a username/password form POST and establishes a session.

    Runs at ``HIGHEST_PRECEDENCE + 230`` — after the session-restoring filter
    (``+225``) so a successful login overrides any prior anonymous context.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 230

    def __init__(
        self,
        authentication_manager: ProviderManager,
        *,
        login_url: str = "/login",
        username_param: str = "username",
        password_param: str = "password",
        success_url: str = "/",
        failure_url: str = "/login?error",
        use_redirect: bool = True,
    ) -> None:
        self._manager = authentication_manager
        self._login_url = login_url
        self._username_param = username_param
        self._password_param = password_param
        self._success_url = success_url
        self._failure_url = failure_url
        self._use_redirect = use_redirect

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        if request.method == "POST" and request.url.path == self._login_url:
            return await self._attempt_login(request)
        return await call_next(request)  # type: ignore[no-any-return]

    async def _attempt_login(self, request: Request) -> Response:
        form = await request.form()
        username = str(form.get(self._username_param, "") or "")
        password = str(form.get(self._password_param, "") or "")

        try:
            result = await self._manager.authenticate(Authentication(principal=username, credentials=password))
        except AuthenticationException:
            logger.warning("Form login failed for user %r", username)
            return self._failure()

        context = result.to_security_context()
        session = getattr(getattr(request, "state", None), "session", None)
        if session is not None:
            # Rotate the session id on authentication to prevent session fixation,
            # then bind the authenticated context to the (new) session.
            session.rotate_id()
            session.set_attribute(_SECURITY_CONTEXT_KEY, context)
        request.state.security_context = context
        logger.info("Form login successful for user: %s", context.user_id)
        return self._success()

    def _success(self) -> Response:
        if self._use_redirect:
            return RedirectResponse(url=self._success_url, status_code=302)
        return JSONResponse({"authenticated": True})

    def _failure(self) -> Response:
        if self._use_redirect:
            return RedirectResponse(url=self._failure_url, status_code=302)
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)
