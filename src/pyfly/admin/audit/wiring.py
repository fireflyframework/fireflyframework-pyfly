# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native post-start provider resolution and read-only HTTP routes."""

from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.admin.audit.config import AdminAuditProperties
from pyfly.admin.audit.provider import AdminAuditProvider
from pyfly.audit.ports import AuditError
from pyfly.kernel.exceptions import ForbiddenException, UnauthorizedException, ValidationException
from pyfly.security.context import SecurityContext


def build_audit_routes(context: Any, base: str, hooks: list[Callable[[], None]]) -> list[Route]:
    if not context.config.bind(AdminAuditProperties).enabled:
        return []
    routes = AdminAuditRoutes()

    def initialize() -> None:
        routes.provider = context.get_bean(AdminAuditProvider)

    if context._started:
        initialize()
    hooks.append(initialize)
    return routes.build(base)


class AdminAuditRoutes:
    def __init__(self) -> None:
        self.provider: AdminAuditProvider | None = None

    def build(self, base: str) -> list[Route]:
        path = base.rstrip("/") + "/api/audit"
        return [Route(path, self.handle, methods=["GET"]), Route(path + "/verify", self.handle, methods=["GET"])]

    async def handle(self, request: Request) -> Response:
        headers = {"Cache-Control": "no-store"}
        try:
            security = getattr(request.state, "security_context", None)
            if not isinstance(security, SecurityContext) or not security.is_authenticated:
                raise UnauthorizedException("Authentication required")
            if self.provider is None:
                return JSONResponse({"error": "Audit reader is not ready"}, status_code=503, headers=headers)
            verify = request.url.path.endswith("/verify")
            allowed = {"scope", "first", "last"} if verify else {"scope", "after", "through", "limit"}
            if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in request.query_params):
                raise ValidationException("Invalid audit query")
            values: dict[str, Any] = {"scope_handle": request.query_params.get("scope")}
            names = (
                {"first": "first_sequence", "last": "last_sequence"}
                if verify
                else {"after": "after_sequence", "through": "through_sequence", "limit": "limit"}
            )
            for key, name in names.items():
                raw = request.query_params.get(key)
                if raw is not None:
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
                        raise ValidationException("Invalid audit query")
                    values[name] = int(raw)
                elif verify:
                    raise ValidationException("Invalid audit range")
            result = await (
                self.provider.verify_range(security, **values) if verify else self.provider.page(security, **values)
            )
            return JSONResponse(result.model_dump(mode="json"), headers=headers)
        except UnauthorizedException:
            return JSONResponse({"error": "Authentication required"}, status_code=401, headers=headers)
        except ForbiddenException:
            return JSONResponse({"error": "Audit access is forbidden"}, status_code=403, headers=headers)
        except ValidationException:
            return JSONResponse({"error": "Invalid audit query"}, status_code=422, headers=headers)
        except AuditError:
            return JSONResponse({"error": "Audit reader is unavailable"}, status_code=503, headers=headers)
