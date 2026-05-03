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
"""Unified exception hierarchy for PyFly.

All framework exceptions inherit from PyFlyException, enabling unified
error handling across modules. Mirrors fireflyframework-kernel's design.

Categories:
- BusinessException: Domain rule violations, validation errors
- SecurityException: Authentication and authorization errors
- InfrastructureException: Database, cache, messaging, network failures
- ExternalServiceException: Third-party and gateway failures
"""

from __future__ import annotations

from typing import Any

# =============================================================================
# Base Exception
# =============================================================================


class PyFlyException(Exception):
    """Base exception for all PyFly errors.

    Carries an optional error code and context dict for structured error data.
    This enables unified exception handling: catch PyFlyException to handle
    all framework errors, or catch specific subclasses for targeted handling.

    Args:
        message: Human-readable error description.
        code: Machine-readable error code (e.g. "VALIDATION_001").
        context: Arbitrary key-value pairs for error context and debugging.
    """

    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context: dict[str, Any] = context if context is not None else {}


# =============================================================================
# Business Exceptions
# =============================================================================


class BusinessException(PyFlyException):
    """Domain rule violations and business logic errors."""


class ValidationException(BusinessException):
    """Input validation failures."""


class ResourceNotFoundException(BusinessException):
    """Requested resource does not exist."""


class ConflictException(BusinessException):
    """Operation conflicts with current state (e.g. duplicate, version mismatch)."""


class PreconditionFailedException(BusinessException):
    """A precondition for the operation was not met."""


class GoneException(BusinessException):
    """Requested resource has been permanently removed."""


class InvalidRequestException(BusinessException):
    """Request is syntactically valid but semantically incorrect."""


class DataIntegrityException(BusinessException):
    """Data integrity constraint violated."""


class ConcurrencyException(BusinessException):
    """Concurrent modification conflict (e.g. optimistic locking failure)."""


class LockedResourceException(BusinessException):
    """Resource is locked and cannot be modified."""


class MethodNotAllowedException(BusinessException):
    """The requested operation or HTTP method is not allowed on this resource."""


class UnsupportedMediaTypeException(BusinessException):
    """The provided media type or content type is not supported."""


class PayloadTooLargeException(BusinessException):
    """The request payload exceeds the maximum allowed size."""


# =============================================================================
# Security Exceptions
# =============================================================================


class SecurityException(PyFlyException):
    """Authentication and authorization errors."""


class UnauthorizedException(SecurityException):
    """Authentication is required but was not provided or is invalid."""


class ForbiddenException(SecurityException):
    """Authenticated caller lacks permission to perform the operation."""


class AuthorizationException(SecurityException):
    """Authorization policy denied access to the requested resource."""


# =============================================================================
# Infrastructure Exceptions
# =============================================================================


class InfrastructureException(PyFlyException):
    """Infrastructure failures: database, cache, messaging, network."""


class ServiceUnavailableException(InfrastructureException):
    """Downstream service is unavailable."""


class CircuitBreakerException(InfrastructureException):
    """Circuit breaker is open, operation rejected."""


class RateLimitException(InfrastructureException):
    """Request rate limit exceeded."""


class BulkheadException(InfrastructureException):
    """Bulkhead capacity exhausted, operation rejected to protect the system."""


class OperationTimeoutException(InfrastructureException):
    """Operation exceeded its allowed time limit."""


class RetryExhaustedException(InfrastructureException):
    """All retry attempts have been exhausted without success."""


class DegradedServiceException(InfrastructureException):
    """Service is running in a degraded state with reduced functionality."""


class NotImplementedException(InfrastructureException):
    """Requested operation is not yet implemented."""


# =============================================================================
# External Service Exceptions
# =============================================================================


class ExternalServiceException(InfrastructureException):
    """Failure communicating with an external or third-party service."""


class ThirdPartyServiceException(ExternalServiceException):
    """A third-party service returned an error or is unavailable."""


class BadGatewayException(ExternalServiceException):
    """Gateway received an invalid response from an upstream service."""


class GatewayTimeoutException(ExternalServiceException):
    """Gateway did not receive a timely response from an upstream service."""


class QuotaExceededException(RateLimitException):
    """API or resource quota has been exceeded."""
