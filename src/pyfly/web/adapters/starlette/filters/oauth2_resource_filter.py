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
"""OAuth2 Resource Server filter — validates JWKS-signed Bearer tokens."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.request_context import RequestContext
from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.resource_server import JWKSTokenValidator
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)


@order(HIGHEST_PRECEDENCE + 250)
class OAuth2ResourceServerFilter(OncePerRequestFilter):
    """Extracts Bearer token and validates it against a JWKS endpoint.

    Populates ``request.state.security_context`` with claims from the JWT.
    Uses ``exclude_patterns`` (fnmatch globs) to skip public endpoints.
    For missing or invalid tokens, sets an anonymous SecurityContext.
    """

    def __init__(
        self,
        token_validator: JWKSTokenValidator,
        exclude_patterns: Sequence[str] = (),
    ) -> None:
        self._token_validator = token_validator
        self.exclude_patterns = list(exclude_patterns)

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]  # len("Bearer ") == 7
            try:
                security_context = self._token_validator.to_security_context(token)
            except SecurityException:
                logger.debug("Invalid OAuth2 token, using anonymous context")
                security_context = SecurityContext.anonymous()
        else:
            security_context = SecurityContext.anonymous()

        request.state.security_context = security_context
        req_ctx = RequestContext.current()
        if req_ctx is not None:
            req_ctx.security_context = security_context
        return cast(Response, await call_next(request))
