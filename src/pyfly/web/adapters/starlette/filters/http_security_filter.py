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
"""HttpSecurityFilter — enforces URL-level authorization rules from HttpSecurity.

Runs **after** authentication filters (SecurityFilter, OAuth2ResourceServerFilter)
and **before** the route handler, rejecting requests that do not satisfy the
configured access rules.

Error responses follow RFC 7807 "problem detail" structure.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from fnmatch import fnmatch
from typing import cast

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.security.context import SecurityContext
from pyfly.security.http_security import AccessRuleType, SecurityRule
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

logger = logging.getLogger(__name__)


def _problem_response(*, status: int, title: str, detail: str, path: str) -> JSONResponse:
    """Build an RFC 7807 problem-detail JSON response."""
    return JSONResponse(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
        },
        status_code=status,
        media_type="application/problem+json",
    )


def _matches(path: str, patterns: list[str]) -> bool:
    """Return ``True`` if *path* matches any of the fnmatch *patterns*.

    An empty patterns list is treated as a wildcard that matches everything
    (used by ``any_request()``).
    """
    if not patterns:
        return True
    return any(fnmatch(path, p) for p in patterns)


@order(HIGHEST_PRECEDENCE + 350)
class HttpSecurityFilter(OncePerRequestFilter):
    """Filter that enforces URL-pattern authorization rules.

    Rules are evaluated in declaration order; **first match wins**.
    If no rule matches, the request is allowed through (open by default).

    Constructed via :meth:`HttpSecurity.build` rather than directly.
    """

    def __init__(self, rules: Sequence[SecurityRule]) -> None:
        self._rules = list(rules)

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        path: str = request.url.path
        security_context: SecurityContext = getattr(request.state, "security_context", SecurityContext.anonymous())

        for security_rule in self._rules:
            if not _matches(path, security_rule.patterns):
                continue

            rule = security_rule.rule
            rule_type = rule.rule_type

            if rule_type is AccessRuleType.PERMIT_ALL:
                return cast(Response, await call_next(request))

            if rule_type is AccessRuleType.DENY_ALL:
                logger.debug("Access denied (DENY_ALL) for path %s", path)
                return _problem_response(
                    status=403,
                    title="Forbidden",
                    detail="Access to this resource is denied.",
                    path=path,
                )

            if rule_type is AccessRuleType.AUTHENTICATED:
                if not security_context.is_authenticated:
                    logger.debug("Unauthenticated request for path %s", path)
                    return _problem_response(
                        status=401,
                        title="Unauthorized",
                        detail="Authentication is required to access this resource.",
                        path=path,
                    )
                return cast(Response, await call_next(request))

            if rule_type is AccessRuleType.HAS_ROLE:
                if not security_context.is_authenticated:
                    return _problem_response(
                        status=401,
                        title="Unauthorized",
                        detail="Authentication is required to access this resource.",
                        path=path,
                    )
                role = cast(str, rule.value)
                if not security_context.has_role(role):
                    logger.debug("Missing role %r for path %s", role, path)
                    return _problem_response(
                        status=403,
                        title="Forbidden",
                        detail=f"Required role '{role}' is not granted.",
                        path=path,
                    )
                return cast(Response, await call_next(request))

            if rule_type is AccessRuleType.HAS_ANY_ROLE:
                if not security_context.is_authenticated:
                    return _problem_response(
                        status=401,
                        title="Unauthorized",
                        detail="Authentication is required to access this resource.",
                        path=path,
                    )
                roles = cast(list[str], rule.value)
                if not security_context.has_any_role(roles):
                    logger.debug("Missing any of roles %r for path %s", roles, path)
                    return _problem_response(
                        status=403,
                        title="Forbidden",
                        detail=f"One of roles {roles} is required.",
                        path=path,
                    )
                return cast(Response, await call_next(request))

            if rule_type is AccessRuleType.HAS_PERMISSION:
                if not security_context.is_authenticated:
                    return _problem_response(
                        status=401,
                        title="Unauthorized",
                        detail="Authentication is required to access this resource.",
                        path=path,
                    )
                permission = cast(str, rule.value)
                if not security_context.has_permission(permission):
                    logger.debug("Missing permission %r for path %s", permission, path)
                    return _problem_response(
                        status=403,
                        title="Forbidden",
                        detail=f"Required permission '{permission}' is not granted.",
                        path=path,
                    )
                return cast(Response, await call_next(request))

        # No rule matched — allow through (open by default)
        return cast(Response, await call_next(request))
