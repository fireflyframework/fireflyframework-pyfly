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
"""Secured JSON API used by the model administration dashboard."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pyfly.admin.data.config import AdminDataProperties
from pyfly.admin.data.models import AdminOperationContext, AdminQuery
from pyfly.admin.data.service import AdminDataService
from pyfly.kernel.exceptions import ForbiddenException, PyFlyException, UnauthorizedException, ValidationException
from pyfly.security.context import SecurityContext
from pyfly.security.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, validate_csrf_token

_logger = logging.getLogger("pyfly.admin.data")


class DataAdminRoutes:
    __pyfly_rest_controller__ = True

    def __init__(self, properties: AdminDataProperties) -> None:
        self.properties = properties
        self.service: AdminDataService | None = None

    def build(self, base: str) -> list[Route]:
        api = base.rstrip("/") + "/api/data"
        return [
            Route(api + "/sources", self.handle, methods=["GET"], name="admin-data-sources"),
            Route(api + "/resources/{resource}/schema", self.handle, methods=["GET"], name="admin-data-schema"),
            Route(
                api + "/resources/{resource}/records", self.handle, methods=["GET", "POST"], name="admin-data-records"
            ),
            Route(
                api + "/resources/{resource}/records/{id:path}",
                self.handle,
                methods=["GET", "PATCH", "DELETE"],
                name="admin-data-record",
            ),
        ]

    def _context(self, request: Request) -> AdminOperationContext:
        security = getattr(request.state, "security_context", None)
        if not isinstance(security, SecurityContext) or not security.is_authenticated:
            raise UnauthorizedException("Authentication required")
        if not security.has_any_role(self.properties.allowed_roles):
            raise ForbiddenException("Data administration is forbidden")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            cookie = request.cookies.get(CSRF_COOKIE_NAME)
            token = request.headers.get(CSRF_HEADER_NAME)
            if (
                not cookie
                or not token
                or not cookie.isascii()
                or not token.isascii()
                or not validate_csrf_token(cookie, token)
            ):
                raise ForbiddenException("A valid CSRF token is required")
        return AdminOperationContext(security, getattr(request.state, "transaction_id", ""))

    async def _body(self, request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise ValidationException("Expected application/json")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > self.properties.max_body_size:
                raise ValidationException("Request body is too large")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValidationException("Invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
            raise ValidationException("Expected an object with values")
        if not set(payload) <= {"values", "editToken"}:
            raise ValidationException("Unknown request properties")
        return payload

    async def handle(self, request: Request) -> Response:
        try:
            context = self._context(request)
            if self.service is None:
                return JSONResponse({"error": "Data administration is not ready"}, status_code=503)
            service = self.service
            resource_id = request.path_params.get("resource")
            if resource_id is None:
                sources: dict[str, list[dict[str, Any]]] = {}
                for resource in service.registry.resources():
                    if (
                        "list" not in service.properties.operations
                        or "list" not in resource.operations
                        or not resource.has_permission("list", context)
                    ):
                        continue
                    sources.setdefault(resource.datasource, []).append(
                        {
                            "id": resource.resource_id,
                            "label": resource.label or resource.resource_id,
                        }
                    )
                return JSONResponse(
                    {"sources": [{"id": name, "resources": entries} for name, entries in sources.items()]}
                )
            if "id" not in request.path_params and request.url.path.endswith("/schema"):
                fields = await service.schema(resource_id, context)
                resource = service.registry.get(resource_id)
                return JSONResponse(
                    {
                        "fields": [asdict(field) for field in fields],
                        "label": resource.label or resource_id,
                        "operations": [
                            op
                            for op in resource.operations
                            if op in service.properties.operations and resource.has_permission(op, context)
                        ],
                        "searchFields": resource.search_fields,
                        "filterFields": resource.filter_fields,
                        "pageSize": service.properties.page_size,
                        "maxPageSize": service.properties.max_page_size,
                    }
                )
            id = request.path_params.get("id")
            if request.method == "GET":
                if id is not None:
                    return JSONResponse(asdict(await service.get(resource_id, id, context)))
                try:
                    filters = json.loads(request.query_params.get("filters", "{}"))
                    if not isinstance(filters, dict):
                        raise ValueError("filters")
                    query = AdminQuery(
                        page=int(request.query_params.get("page", "1")),
                        size=int(request.query_params.get("size", str(service.properties.page_size))),
                        search=request.query_params.get("search", ""),
                        sort=tuple(filter(None, request.query_params.get("sort", "").split(","))),
                        filters=filters,
                    )
                except (ValueError, TypeError) as exc:
                    raise ValidationException("Invalid query parameters") from exc
                return JSONResponse(asdict(await service.list(resource_id, query, context)))
            if request.method == "DELETE" and id is not None:
                await service.delete(resource_id, id, request.headers.get("if-match", "").strip('"'), context)
                return Response(status_code=204)
            payload = await self._body(request)
            if request.method == "POST":
                record = await service.create(resource_id, payload["values"], context)
                return JSONResponse(asdict(record), status_code=201)
            token = payload.get("editToken", "")
            if not isinstance(token, str) or not token.isascii():
                raise ValidationException("Invalid edit token")
            record = await service.update(resource_id, str(id), payload["values"], token, context)
            return JSONResponse(asdict(record))
        except PyFlyException as exc:
            from pyfly.web.adapters.starlette.errors import _get_status_code

            status = _get_status_code(exc)
            security = getattr(request.state, "security_context", None)
            logging.getLogger("pyfly.admin.data.audit").info(
                "admin_request actor=%r resource=%r id=%r method=%s status=%s correlation_id=%r result=denied",
                getattr(security, "user_id", None),
                request.path_params.get("resource"),
                request.path_params.get("id"),
                request.method,
                status,
                getattr(request.state, "transaction_id", ""),
            )
            return JSONResponse({"error": str(exc), "errors": exc.context.get("errors", [])}, status_code=status)
        except Exception:
            _logger.exception("admin_data_failure")
            return JSONResponse({"error": "Internal data administration error"}, status_code=500)
