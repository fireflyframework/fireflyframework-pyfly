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


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all exceptions with structured JSON responses."""
    transaction_id = getattr(request.state, "transaction_id", str(uuid.uuid4()))
    timestamp = datetime.now(UTC).isoformat()

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
            body["error"]["context"] = exc.context
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
