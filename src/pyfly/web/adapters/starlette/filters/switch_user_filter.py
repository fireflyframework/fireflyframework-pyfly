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
"""switch-user / run-as impersonation (Spring ``SwitchUserFilter``).

An authorized principal (holding ``switch_authority``) may impersonate another
user by visiting the switch URL; the original principal is stashed in the session
and restored at the exit URL. While impersonating, the session context carries the
:data:`PREVIOUS_PRINCIPAL_ROLE` marker so the application can detect run-as.
"""

from __future__ import annotations

import logging
from typing import cast

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.security.context import SecurityContext
from pyfly.security.user_details import UserDetailsService
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"
_ORIGINAL_CONTEXT_KEY = "SWITCH_USER_ORIGINAL"

#: Authority granted to an impersonated context so the app can detect run-as
#: and offer an "exit" action (cf. Spring's ``ROLE_PREVIOUS_ADMINISTRATOR``).
PREVIOUS_PRINCIPAL_ROLE = "PREVIOUS_ADMINISTRATOR"


class SwitchUserFilter(OncePerRequestFilter):
    """Lets an authorized principal impersonate another user, and switch back.

    Runs at ``HIGHEST_PRECEDENCE + 232`` (after form login, before logout).
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 232

    def __init__(
        self,
        user_details_service: UserDetailsService,
        *,
        switch_url: str = "/login/impersonate",
        exit_url: str = "/logout/impersonate",
        username_param: str = "username",
        switch_authority: str = "ADMIN",
        success_url: str = "/",
    ) -> None:
        self._users = user_details_service
        self._switch_url = switch_url
        self._exit_url = exit_url
        self._username_param = username_param
        self._switch_authority = switch_authority
        self._success_url = success_url

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        path = request.url.path
        if path == self._switch_url:
            return await self._switch(request)
        if path == self._exit_url:
            return self._exit(request)
        return cast(Response, await call_next(request))

    def _current_context(self, request: Request) -> SecurityContext | None:
        session = getattr(getattr(request, "state", None), "session", None)
        if session is not None:
            stored = session.get_attribute(_SECURITY_CONTEXT_KEY)
            if isinstance(stored, SecurityContext):
                return stored
        ctx = getattr(getattr(request, "state", None), "security_context", None)
        return ctx if isinstance(ctx, SecurityContext) else None

    async def _switch(self, request: Request) -> Response:
        current = self._current_context(request)
        if current is None or not current.is_authenticated:
            return JSONResponse({"error": "authentication_required"}, status_code=401)
        if not (current.has_role(self._switch_authority) or current.has_permission(self._switch_authority)):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        target_username = request.query_params.get(self._username_param, "")
        user = await self._users.load_user_by_username(target_username) if target_username else None
        if user is None or not user.enabled:
            return JSONResponse({"error": "user_not_found"}, status_code=404)

        impersonated = SecurityContext(
            user_id=user.username,
            roles=[*user.roles, PREVIOUS_PRINCIPAL_ROLE],
            permissions=list(user.permissions),
            attributes={"switch_user_original": current.user_id or ""},
        )
        session = request.state.session
        session.set_attribute(_ORIGINAL_CONTEXT_KEY, current)
        session.set_attribute(_SECURITY_CONTEXT_KEY, impersonated)
        request.state.security_context = impersonated
        logger.info("User %s is now impersonating %s", current.user_id, user.username)
        return RedirectResponse(url=self._success_url, status_code=302)

    def _exit(self, request: Request) -> Response:
        session = getattr(getattr(request, "state", None), "session", None)
        if session is None:
            return JSONResponse({"error": "not_impersonating"}, status_code=400)
        original = session.get_attribute(_ORIGINAL_CONTEXT_KEY)
        if not isinstance(original, SecurityContext):
            return JSONResponse({"error": "not_impersonating"}, status_code=400)
        session.set_attribute(_SECURITY_CONTEXT_KEY, original)
        session.remove_attribute(_ORIGINAL_CONTEXT_KEY)
        request.state.security_context = original
        logger.info("Impersonation ended; restored principal %s", original.user_id)
        return RedirectResponse(url=self._success_url, status_code=302)
