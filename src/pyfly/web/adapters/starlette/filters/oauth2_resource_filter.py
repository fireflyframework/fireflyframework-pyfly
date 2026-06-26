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
from typing import Any, cast

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.request_context import RequestContext
from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.resource_server import JWKSTokenValidator
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)

# RFC 6750 §3 challenge returned in "401" error mode for an invalid token.
_INVALID_TOKEN_CHALLENGE = 'Bearer error="invalid_token"'

ERROR_MODE_ANONYMOUS = "anonymous"
ERROR_MODE_401 = "401"


@order(HIGHEST_PRECEDENCE + 250)
class OAuth2ResourceServerFilter(OncePerRequestFilter):
    """Extracts the Bearer token and validates it against a JWKS endpoint.

    Populates ``request.state.security_context`` (and the active
    :class:`RequestContext`) with claims from the JWT. ``exclude_patterns``
    (fnmatch globs, honoured by :class:`OncePerRequestFilter`) skip public paths.

    Behaviour on a bad/missing token is governed by ``error_mode``:

    * ``"anonymous"`` (default): an invalid **or** missing token yields an
      anonymous :class:`SecurityContext` and the request proceeds — the
      downstream ``HttpSecurity`` gate / ``@pre_authorize`` decide. This keeps
      the resource-server filter composable with permit-all public endpoints.
    * ``"401"``: a **present but invalid** token is rejected here with
      ``401 Unauthorized`` and a ``WWW-Authenticate: Bearer error="invalid_token"``
      header (RFC 6750). A **missing** token still falls through to the gate
      (so public endpoints remain reachable).

    JWKS key resolution does blocking network I/O on a cache miss, so token
    validation runs in a worker thread (``anyio.to_thread``) to avoid stalling
    the event loop.
    """

    def __init__(
        self,
        token_validator: JWKSTokenValidator,
        exclude_patterns: Sequence[str] = (),
        *,
        error_mode: str = ERROR_MODE_ANONYMOUS,
        enforce_sender_constraints: bool = False,
        dpop_validator: Any = None,
        mtls_cert_header: str = "x-client-cert",
    ) -> None:
        self._token_validator = token_validator
        self.exclude_patterns = list(exclude_patterns)
        self._error_mode = error_mode if error_mode in (ERROR_MODE_ANONYMOUS, ERROR_MODE_401) else ERROR_MODE_ANONYMOUS
        self._enforce_sc = enforce_sender_constraints
        self._dpop_validator = dpop_validator
        self._mtls_cert_header = mtls_cert_header

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        token = self._extract_token(request.headers.get("authorization", ""))

        if token is not None:
            try:
                # Offload to a worker thread: JWKS key lookup may do blocking
                # urllib I/O on a cache miss, which would otherwise stall the loop.
                if self._enforce_sc:
                    payload, security_context = await to_thread.run_sync(
                        self._token_validator.validate_and_context, token
                    )
                    # Sender-constrained tokens (RFC 9449 DPoP / RFC 8705 mTLS) must be
                    # accompanied by proof of possession; a stolen token alone is useless.
                    self._enforce_sender_constraint(request, payload, token)
                else:
                    security_context = await to_thread.run_sync(self._token_validator.to_security_context, token)
            except SecurityException:
                # A token was presented but failed validation (bad signature,
                # expired, wrong iss/aud, unknown kid, failed proof-of-possession).
                logger.warning("OAuth2 token rejected (invalid_token)")
                if self._error_mode == ERROR_MODE_401:
                    return self._invalid_token_response()
                security_context = SecurityContext.anonymous()
        else:
            # No bearer credentials presented — anonymous; the gate decides.
            security_context = SecurityContext.anonymous()

        request.state.security_context = security_context
        req_ctx = RequestContext.current()
        if req_ctx is not None:
            req_ctx.security_context = security_context
        return cast(Response, await call_next(request))

    def _enforce_sender_constraint(self, request: Request, payload: dict[str, Any], token: str) -> None:
        """Enforce DPoP/mTLS proof-of-possession when the token carries a ``cnf`` claim."""
        cnf = payload.get("cnf")
        if not isinstance(cnf, dict):
            return  # plain bearer token — nothing to enforce
        if "jkt" in cnf:
            from urllib.parse import urlsplit, urlunsplit

            from pyfly.security.oauth2.dpop import DPoPProofValidator, confirm_dpop_binding

            proof = request.headers.get("dpop")
            if not proof:
                raise SecurityException("DPoP proof required for this token", code="INVALID_TOKEN")
            validator = self._dpop_validator or DPoPProofValidator()
            parts = urlsplit(str(request.url))
            http_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            jkt = validator.validate(proof, http_method=request.method, http_url=http_url, access_token=token)
            confirm_dpop_binding(payload, jkt)
        elif "x5t#S256" in cnf:
            from urllib.parse import unquote

            from pyfly.security.oauth2.dpop import confirm_mtls_binding

            cert = request.headers.get(self._mtls_cert_header)
            if not cert:
                raise SecurityException("Client certificate required for this token", code="INVALID_TOKEN")
            confirm_mtls_binding(payload, unquote(cert))

    @staticmethod
    def _extract_token(auth_header: str) -> str | None:
        """Return the token from a ``Bearer`` or ``DPoP`` ``Authorization`` header.

        The auth scheme is matched case-insensitively (RFC 7235 §2.1).
        """
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "dpop") and parts[1].strip():
            return parts[1].strip()
        return None

    @staticmethod
    def _invalid_token_response() -> Response:
        return JSONResponse(
            {"error": "invalid_token", "error_description": "The access token is invalid or expired."},
            status_code=401,
            headers={"WWW-Authenticate": _INVALID_TOKEN_CHALLENGE},
        )
