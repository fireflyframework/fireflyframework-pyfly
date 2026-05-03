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
"""OAuth2 Session Security Filter — restores SecurityContext from session."""

from __future__ import annotations

import logging
from typing import Any

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.security.context import SecurityContext
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

_SECURITY_CONTEXT_KEY = "SECURITY_CONTEXT"


class OAuth2SessionSecurityFilter(OncePerRequestFilter):
    """Restores a :class:`SecurityContext` from the HTTP session.

    Runs at ``HIGHEST_PRECEDENCE + 225``, which is *before* the JWT-based
    ``SecurityFilter`` (at ``+250``) and the ``OAuth2ResourceServerFilter``
    (at ``+250``).  This ensures session-based authentication takes priority
    over token-based authentication.

    If a ``SECURITY_CONTEXT`` attribute is found in the session, it is set on
    ``request.state.security_context``.  Otherwise an anonymous context is
    set so downstream filters and handlers always have a context available.
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE + 225

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        session = getattr(getattr(request, "state", None), "session", None)

        if session is not None:
            stored_ctx = session.get_attribute(_SECURITY_CONTEXT_KEY)
            if isinstance(stored_ctx, SecurityContext) and stored_ctx.is_authenticated:
                request.state.security_context = stored_ctx
                logger.debug("Restored SecurityContext from session for user: %s", stored_ctx.user_id)
                return await call_next(request)

        # No session-based context — set anonymous so downstream filters can override
        if not hasattr(request.state, "security_context"):
            request.state.security_context = SecurityContext.anonymous()

        return await call_next(request)
