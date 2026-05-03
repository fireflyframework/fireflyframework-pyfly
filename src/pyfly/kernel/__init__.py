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
"""PyFly Kernel — Foundation layer with zero external dependencies."""

from pyfly.kernel.exceptions import (
    AuthorizationException,
    BadGatewayException,
    BulkheadException,
    BusinessException,
    CircuitBreakerException,
    ConcurrencyException,
    ConflictException,
    DataIntegrityException,
    DegradedServiceException,
    ExternalServiceException,
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
    RetryExhaustedException,
    SecurityException,
    ServiceUnavailableException,
    ThirdPartyServiceException,
    UnauthorizedException,
    UnsupportedMediaTypeException,
    ValidationException,
)
from pyfly.kernel.lifecycle import Lifecycle
from pyfly.kernel.types import (
    ErrorCategory,
    ErrorResponse,
    ErrorSeverity,
    FieldError,
)

__all__ = [
    # Lifecycle
    "Lifecycle",
    # Types
    "ErrorCategory",
    "ErrorSeverity",
    "FieldError",
    "ErrorResponse",
    # Base
    "PyFlyException",
    # Business
    "BusinessException",
    "ValidationException",
    "ResourceNotFoundException",
    "ConflictException",
    "PreconditionFailedException",
    "GoneException",
    "InvalidRequestException",
    "DataIntegrityException",
    "ConcurrencyException",
    "LockedResourceException",
    "MethodNotAllowedException",
    "UnsupportedMediaTypeException",
    "PayloadTooLargeException",
    # Security
    "SecurityException",
    "UnauthorizedException",
    "ForbiddenException",
    "AuthorizationException",
    # Infrastructure
    "InfrastructureException",
    "ServiceUnavailableException",
    "CircuitBreakerException",
    "RateLimitException",
    "BulkheadException",
    "OperationTimeoutException",
    "RetryExhaustedException",
    "DegradedServiceException",
    "NotImplementedException",
    # External Service
    "ExternalServiceException",
    "ThirdPartyServiceException",
    "BadGatewayException",
    "GatewayTimeoutException",
    "QuotaExceededException",
]
