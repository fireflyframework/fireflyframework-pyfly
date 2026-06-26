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
"""X.509 client-certificate authentication (Spring ``x509()``).

Authenticates a request by the client certificate forwarded by the TLS-terminating
proxy in a header (e.g. ``X-Client-Cert``, PEM, possibly URL-encoded). The
certificate subject's Common Name becomes the principal; when a
:class:`~pyfly.security.user_details.UserDetailsService` is configured, the
principal must resolve to a (enabled) user, whose authorities are applied.
"""

from __future__ import annotations

import logging
import re
from typing import cast
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from pyfly.security.user_details import UserDetailsService
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

ERROR_MODE_ANONYMOUS = "anonymous"
ERROR_MODE_401 = "401"


@order(HIGHEST_PRECEDENCE + 218)
class X509AuthenticationFilter(OncePerRequestFilter):
    """Authenticates the forwarded client certificate's subject."""

    def __init__(
        self,
        *,
        cert_header: str = "x-client-cert",
        user_details_service: UserDetailsService | None = None,
        subject_regex: str | None = None,
        error_mode: str = ERROR_MODE_ANONYMOUS,
    ) -> None:
        self._cert_header = cert_header
        self._users = user_details_service
        self._subject_regex = re.compile(subject_regex) if subject_regex else None
        self._error_mode = error_mode if error_mode in (ERROR_MODE_ANONYMOUS, ERROR_MODE_401) else ERROR_MODE_ANONYMOUS

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        raw = request.headers.get(self._cert_header)
        if not raw:
            if not hasattr(request.state, "security_context"):
                request.state.security_context = SecurityContext.anonymous()
            return cast(Response, await call_next(request))

        principal = self._extract_principal(unquote(raw))
        context = await self._build_context(principal) if principal else None

        if context is None:
            logger.warning("X.509 authentication failed (header=%s)", self._cert_header)
            if self._error_mode == ERROR_MODE_401:
                return self._unauthorized()
            context = SecurityContext.anonymous()

        request.state.security_context = context
        req_ctx = RequestContext.current()
        if req_ctx is not None:
            req_ctx.security_context = context
        return cast(Response, await call_next(request))

    def _extract_principal(self, pem: str) -> str | None:
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID

            cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
        except Exception:  # malformed / non-PEM certificate
            return None
        if self._subject_regex is not None:
            match = self._subject_regex.search(cert.subject.rfc4514_string())
            return match.group(1) if match and match.groups() else None
        common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(common_names[0].value) if common_names else None

    async def _build_context(self, principal: str) -> SecurityContext | None:
        if self._users is None:
            # Certificate presence is the credential; no authority lookup.
            return SecurityContext(user_id=principal)
        user = await self._users.load_user_by_username(principal)
        if user is None or not user.enabled:
            return None
        return SecurityContext(user_id=user.username, roles=list(user.roles), permissions=list(user.permissions))

    @staticmethod
    def _unauthorized() -> Response:
        return JSONResponse(
            {"error": "invalid_client_certificate"},
            status_code=401,
            headers={"WWW-Authenticate": "X509"},
        )
