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
"""Global exception handler — RFC 7807 inspired error responses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from pyfly.kernel.exceptions import (
    BadGatewayException,
    BulkheadException,
    BusinessException,
    CircuitBreakerException,
    ConflictException,
    DegradedServiceException,
    ForbiddenException,
    GatewayTimeoutException,
    GoneException,
    InfrastructureException,
    InvalidRequestException,
    LockedResourceException,
    MethodNotAllowedException,
    NotImplementedException,
    OperationTimeoutException,
    PayloadTooLargeException,
    PreconditionFailedException,
    PyFlyException,
    QuotaExceededException,
    RateLimitException,
    ResourceNotFoundException,
    SecurityException,
    ServiceUnavailableException,
    UnauthorizedException,
    UnsupportedMediaTypeException,
    ValidationException,
)


def _json_safe(value: Any) -> Any:
    """Coerce *value* into a JSON-serializable form, stringifying anything
    ``json.dumps`` cannot handle.

    Pydantic validation errors embed non-serializable members in ``ctx`` (e.g.
    the ``ValueError`` raised by a custom field validator); without this the
    error envelope would crash to a bare 500 instead of rendering the 422.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# Exception -> HTTP status code mapping (most specific first)
_STATUS_MAP: dict[type, int] = {
    # Business
    ValidationException: 422,
    ResourceNotFoundException: 404,
    ConflictException: 409,
    PreconditionFailedException: 412,
    GoneException: 410,
    InvalidRequestException: 400,
    LockedResourceException: 423,
    MethodNotAllowedException: 405,
    UnsupportedMediaTypeException: 415,
    PayloadTooLargeException: 413,
    # Security
    UnauthorizedException: 401,
    ForbiddenException: 403,
    SecurityException: 403,
    # Rate limiting
    QuotaExceededException: 429,
    RateLimitException: 429,
    # Resilience
    CircuitBreakerException: 503,
    BulkheadException: 503,
    ServiceUnavailableException: 503,
    DegradedServiceException: 503,
    OperationTimeoutException: 504,
    NotImplementedException: 501,
    # External
    BadGatewayException: 502,
    GatewayTimeoutException: 504,
    # Catch-all
    BusinessException: 400,
    InfrastructureException: 502,
}


def _get_status_code(exc: Exception) -> int:
    """Map exception type to HTTP status code."""
    for exc_type, status in _STATUS_MAP.items():
        if isinstance(exc, exc_type):
            return status
    return 500


def _try_convert(request: Request, exc: Exception) -> PyFlyException | None:
    """Translate a non-PyFly exception via the registered converter chain.

    Reads the per-app ``ExceptionConverterService`` stashed on ``app.state`` by
    ``create_app``, falling back to the built-in default chain. Returns the
    converted PyFly exception, or ``None`` when no converter matches (audit #202).
    """
    service = getattr(getattr(request, "app", None), "state", None)
    service = getattr(service, "pyfly_exception_converter_service", None)
    if service is None:
        from pyfly.web.converters import build_exception_converter_service

        service = build_exception_converter_service()
    try:
        return service.convert(exc)
    except Exception:  # noqa: BLE001 — a converter must never mask the original error
        return None


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all exceptions with structured JSON responses."""
    transaction_id = getattr(request.state, "transaction_id", str(uuid.uuid4()))
    timestamp = datetime.now(UTC).isoformat()

    # Translate known library exceptions (Pydantic, JSON, timeout, user-registered)
    # into the appropriate PyFly exception before deciding the status (audit #202).
    if not isinstance(exc, PyFlyException):
        converted = _try_convert(request, exc)
        if converted is not None:
            exc = converted

    if isinstance(exc, PyFlyException):
        status = _get_status_code(exc)
        body: dict[str, Any] = {
            "error": {
                "message": str(exc),
                "code": exc.code or type(exc).__name__,
                "transaction_id": transaction_id,
                "timestamp": timestamp,
                "status": status,
                "path": request.url.path,
            }
        }
        if exc.context:
            body["error"]["context"] = _json_safe(exc.context)
    else:
        status = 500
        body = {
            "error": {
                "message": "Internal server error",
                "code": "INTERNAL_ERROR",
                "transaction_id": transaction_id,
                "timestamp": timestamp,
                "status": status,
                "path": request.url.path,
            }
        }

    return JSONResponse(body, status_code=status)
