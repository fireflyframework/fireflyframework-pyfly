# Error Handling Guide

PyFly provides a comprehensive, structured approach to error handling. Every
framework exception carries machine-readable metadata, maps automatically to the
correct HTTP status code, and produces RFC 7807-inspired error responses. This guide
covers the full exception hierarchy, the error response model, and strategies for
using them effectively in your services.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Exception Hierarchy](#exception-hierarchy)
   - [Tree Diagram](#tree-diagram)
3. [PyFlyException](#pyflyexception)
4. [Business Exceptions](#business-exceptions)
   - [BusinessException](#businessexception)
   - [ValidationException](#validationexception)
   - [ResourceNotFoundException](#resourcenotfoundexception)
   - [ConflictException](#conflictexception)
   - [PreconditionFailedException](#preconditionfailedexception)
   - [GoneException](#goneexception)
   - [InvalidRequestException](#invalidrequestexception)
   - [DataIntegrityException](#dataintegrityexception)
   - [ConcurrencyException](#concurrencyexception)
   - [LockedResourceException](#lockedresourceexception)
   - [MethodNotAllowedException](#methodnotallowedexception)
   - [UnsupportedMediaTypeException](#unsupportedmediatypeexception)
   - [PayloadTooLargeException](#payloadtoolargeexception)
5. [Security Exceptions](#security-exceptions)
   - [SecurityException](#securityexception)
   - [UnauthorizedException](#unauthorizedexception)
   - [ForbiddenException](#forbiddenexception)
   - [AuthorizationException](#authorizationexception)
6. [Infrastructure Exceptions](#infrastructure-exceptions)
   - [InfrastructureException](#infrastructureexception)
   - [ServiceUnavailableException](#serviceunavailableexception)
   - [CircuitBreakerException](#circuitbreakerexception)
   - [RateLimitException](#ratelimitexception)
   - [QuotaExceededException](#quotaexceededexception)
   - [BulkheadException](#bulkheadexception)
   - [OperationTimeoutException](#operationtimeoutexception)
   - [RetryExhaustedException](#retryexhaustedexception)
   - [DegradedServiceException](#degradedserviceexception)
   - [NotImplementedException](#notimplementedexception)
7. [External Service Exceptions](#external-service-exceptions)
   - [ExternalServiceException](#externalserviceexception)
   - [ThirdPartyServiceException](#thirdpartyserviceexception)
   - [BadGatewayException](#badgatewayexception)
   - [GatewayTimeoutException](#gatewaytimeoutexception)
8. [HTTP Status Mapping](#http-status-mapping)
9. [ErrorResponse](#errorresponse)
   - [Core Fields](#core-fields)
   - [Optional Fields](#optional-fields)
   - [to_dict() Method](#to_dict-method)
10. [ErrorCategory Enum](#errorcategory-enum)
11. [ErrorSeverity Enum](#errorseverity-enum)
12. [FieldError Dataclass](#fielderror-dataclass)
13. [Complete Example](#complete-example)

---

## Introduction

PyFly's error handling philosophy is built on four principles:

1. **Structured exceptions.** Every exception carries a `message`, `code`, and
   `context` dict -- not just a string. This enables machine-readable error handling
   at every layer.

2. **Categorical hierarchy.** Exceptions are organized by domain: business logic,
   security, infrastructure, and external services. Catch an entire category or a
   specific exception.

3. **Automatic HTTP mapping.** The web layer's global exception handler maps every
   `PyFlyException` subclass to the appropriate HTTP status code. You never need to
   manually set status codes for standard error cases.

4. **RFC 7807-inspired responses.** The `ErrorResponse` dataclass provides a
   comprehensive error payload with tracing, classification, and field-level details.

```python
from pyfly.kernel import (
    # Base
    PyFlyException,
    # Business
    BusinessException, ValidationException, ResourceNotFoundException,
    ConflictException, InvalidRequestException,
    # Security
    SecurityException, UnauthorizedException, ForbiddenException,
    # Infrastructure
    InfrastructureException, ServiceUnavailableException,
    CircuitBreakerException, RateLimitException,
    # Types
    ErrorResponse, ErrorCategory, ErrorSeverity, FieldError,
)
```

---

## Exception Hierarchy

### Tree Diagram

```
PyFlyException
|
+-- BusinessException
|   +-- ValidationException
|   +-- ResourceNotFoundException
|   +-- ConflictException
|   +-- PreconditionFailedException
|   +-- GoneException
|   +-- InvalidRequestException
|   +-- DataIntegrityException
|   +-- ConcurrencyException
|   +-- LockedResourceException
|   +-- MethodNotAllowedException
|   +-- UnsupportedMediaTypeException
|   +-- PayloadTooLargeException
|
+-- SecurityException
|   +-- UnauthorizedException
|   +-- ForbiddenException
|   +-- AuthorizationException
|
+-- InfrastructureException
|   +-- ServiceUnavailableException
|   +-- CircuitBreakerException
|   +-- RateLimitException
|   |   +-- QuotaExceededException
|   +-- BulkheadException
|   +-- OperationTimeoutException
|   +-- RetryExhaustedException
|   +-- DegradedServiceException
|   +-- NotImplementedException
|   |
|   +-- ExternalServiceException
|       +-- ThirdPartyServiceException
|       +-- BadGatewayException
|       +-- GatewayTimeoutException
```

This structure lets you catch at any level of granularity:

```python
try:
    await order_service.create_order(data)
except ValidationException:
    # Handle validation specifically
except BusinessException:
    # Handle any business rule violation
except PyFlyException:
    # Handle any framework exception
```

**Source:** `src/pyfly/kernel/exceptions.py`

---

## PyFlyException

The root of the exception hierarchy. All PyFly exceptions inherit from this class,
enabling unified error handling.

```python
class PyFlyException(Exception):
    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict | None = None,
    ) -> None:
```

**Constructor Parameters:**

| Parameter | Type           | Default | Description                                       |
|-----------|----------------|---------|---------------------------------------------------|
| `message` | `str`          | required | Human-readable error description                 |
| `code`    | `str \| None`  | `None`  | Machine-readable error code (e.g. `"ORDER_001"`) |
| `context` | `dict \| None` | `None`  | Arbitrary key-value pairs for debugging context  |

**Usage:**

```python
raise PyFlyException(
    "Something went wrong",
    code="INTERNAL_ERROR",
    context={"operation": "create_order", "customer_id": "cust-42"},
)
```

**Accessing fields:**

```python
try:
    ...
except PyFlyException as exc:
    print(str(exc))       # "Something went wrong"
    print(exc.code)       # "INTERNAL_ERROR"
    print(exc.context)    # {"operation": "create_order", "customer_id": "cust-42"}
```

---

## Business Exceptions

Business exceptions represent domain rule violations and client errors. They
generally map to 4xx HTTP status codes.

### BusinessException

Base class for all domain logic errors.

```python
raise BusinessException("Order total exceeds credit limit", code="CREDIT_LIMIT")
```

**HTTP Status:** 400 (catch-all for unspecified business exceptions)

### ValidationException

Raised when input data fails validation rules.

```python
raise ValidationException(
    "Invalid order data",
    code="VALIDATION_ERROR",
    context={
        "errors": [
            {"loc": ["quantity"], "msg": "must be positive"},
            {"loc": ["email"], "msg": "invalid format"},
        ]
    },
)
```

**HTTP Status:** 422 Unprocessable Entity

**When to use:** Invalid input fields, Pydantic validation failures, custom
business rule validation.

### ResourceNotFoundException

Raised when a requested entity does not exist.

```python
raise ResourceNotFoundException(
    "Order not found",
    code="ORDER_NOT_FOUND",
    context={"order_id": "ord-999"},
)
```

**HTTP Status:** 404 Not Found

**When to use:** Database lookup returns no result, entity has not been created.

### ConflictException

Raised when an operation conflicts with the current state.

```python
raise ConflictException(
    "Order already exists",
    code="ORDER_DUPLICATE",
    context={"order_id": "ord-001"},
)
```

**HTTP Status:** 409 Conflict

**When to use:** Duplicate creation, version mismatch on update, state machine
violation.

### PreconditionFailedException

Raised when a precondition for the operation was not met.

```python
raise PreconditionFailedException(
    "Order must be in PENDING state to be confirmed",
    code="INVALID_STATE_TRANSITION",
    context={"current_state": "shipped"},
)
```

**HTTP Status:** 412 Precondition Failed

**When to use:** Conditional updates (If-Match header), state preconditions.

### GoneException

Raised when a resource has been permanently removed.

```python
raise GoneException(
    "This order was permanently deleted",
    code="ORDER_DELETED",
    context={"order_id": "ord-001", "deleted_at": "2026-01-01T00:00:00Z"},
)
```

**HTTP Status:** 410 Gone

**When to use:** Soft-deleted resources, expired links, decommissioned endpoints.

### InvalidRequestException

Raised when a request is syntactically valid but semantically incorrect.

```python
raise InvalidRequestException(
    "Cannot ship to a PO Box with express delivery",
    code="INVALID_SHIPPING_COMBO",
)
```

**HTTP Status:** 400 Bad Request

**When to use:** Business logic rejects the request despite it being well-formed.

### DataIntegrityException

Raised when a data integrity constraint is violated.

```python
raise DataIntegrityException(
    "Foreign key constraint: customer does not exist",
    code="FK_VIOLATION",
    context={"customer_id": "cust-nonexistent"},
)
```

**When to use:** Database constraint violations, referential integrity errors.

### ConcurrencyException

Raised on concurrent modification conflicts.

```python
raise ConcurrencyException(
    "Order was modified by another process",
    code="OPTIMISTIC_LOCK_FAILURE",
    context={"expected_version": 3, "actual_version": 5},
)
```

**When to use:** Optimistic locking failures, compare-and-swap mismatches.

### LockedResourceException

Raised when a resource is locked and cannot be modified.

```python
raise LockedResourceException(
    "Order is locked for processing",
    code="ORDER_LOCKED",
    context={"order_id": "ord-001", "locked_by": "batch-job-42"},
)
```

**HTTP Status:** 423 Locked

**When to use:** Pessimistic locking, administrative locks.

### MethodNotAllowedException

Raised when the requested operation is not allowed on the resource.

```python
raise MethodNotAllowedException(
    "Cannot cancel a shipped order",
    code="CANCEL_NOT_ALLOWED",
)
```

**HTTP Status:** 405 Method Not Allowed

### UnsupportedMediaTypeException

Raised when the content type is not supported.

```python
raise UnsupportedMediaTypeException(
    "Only JSON content is accepted",
    code="UNSUPPORTED_CONTENT_TYPE",
)
```

**HTTP Status:** 415 Unsupported Media Type

### PayloadTooLargeException

Raised when the request payload exceeds the maximum allowed size.

```python
raise PayloadTooLargeException(
    "File upload exceeds 10MB limit",
    code="FILE_TOO_LARGE",
    context={"max_size_mb": 10, "actual_size_mb": 25},
)
```

**HTTP Status:** 413 Payload Too Large

---

## Security Exceptions

Security exceptions represent authentication and authorization failures. They map
to 401 and 403 HTTP status codes.

### SecurityException

Base class for all security-related errors.

```python
raise SecurityException("Security violation", code="SECURITY_ERROR")
```

**HTTP Status:** 403 (catch-all)

### UnauthorizedException

Raised when authentication is required but not provided or invalid.

```python
raise UnauthorizedException(
    "Invalid or expired token",
    code="TOKEN_EXPIRED",
    context={"token_type": "Bearer"},
)
```

**HTTP Status:** 401 Unauthorized

**When to use:** Missing credentials, expired token, invalid signature.

### ForbiddenException

Raised when the authenticated caller lacks permission.

```python
raise ForbiddenException(
    "Insufficient permissions to delete orders",
    code="ACCESS_DENIED",
    context={"required_role": "admin", "user_role": "viewer"},
)
```

**HTTP Status:** 403 Forbidden

**When to use:** User is authenticated but not authorized for this action.

### AuthorizationException

Raised when an authorization policy denies access.

```python
raise AuthorizationException(
    "Policy 'org-member-only' denied access",
    code="POLICY_DENIED",
    context={"policy": "org-member-only"},
)
```

**When to use:** Fine-grained policy-based access control decisions.

---

## Infrastructure Exceptions

Infrastructure exceptions represent system-level failures: database outages, circuit
breaker trips, rate limiting, and timeouts.

### InfrastructureException

Base class for all infrastructure errors.

```python
raise InfrastructureException("Database connection pool exhausted")
```

**HTTP Status:** 502 (catch-all)

### ServiceUnavailableException

Raised when a downstream service is unavailable.

```python
raise ServiceUnavailableException(
    "Database is unavailable",
    code="DB_UNAVAILABLE",
)
```

**HTTP Status:** 503 Service Unavailable

### CircuitBreakerException

Raised when a circuit breaker is open.

```python
raise CircuitBreakerException(
    "Circuit breaker open for payment-service",
    code="CIRCUIT_OPEN",
    context={"service": "payment-service", "failures": 5},
)
```

**HTTP Status:** 503 Service Unavailable

### RateLimitException

Raised when a rate limit is exceeded.

```python
raise RateLimitException(
    "Rate limit exceeded: 100 requests per minute",
    code="RATE_LIMIT",
    context={"limit": 100, "window": "1m"},
)
```

**HTTP Status:** 429 Too Many Requests

### QuotaExceededException

Raised when an API or resource quota is exhausted. Inherits from `RateLimitException`.

```python
raise QuotaExceededException(
    "Monthly API quota exceeded",
    code="QUOTA_EXCEEDED",
    context={"quota": 10000, "used": 10001},
)
```

**HTTP Status:** 429 Too Many Requests

### BulkheadException

Raised when bulkhead capacity is exhausted.

```python
raise BulkheadException(
    "Bulkhead capacity exhausted for inventory-service",
    code="BULKHEAD_FULL",
)
```

**HTTP Status:** 503 Service Unavailable

**When to use:** Concurrent request limit reached, protecting the system from
cascade failures.

### OperationTimeoutException

Raised when an operation exceeds its time limit.

```python
raise OperationTimeoutException(
    "Database query timed out after 30s",
    code="QUERY_TIMEOUT",
    context={"timeout_seconds": 30},
)
```

**HTTP Status:** 504 Gateway Timeout

### RetryExhaustedException

Raised when all retry attempts have been exhausted.

```python
raise RetryExhaustedException(
    "Failed after 3 retries to reach inventory-service",
    code="RETRY_EXHAUSTED",
    context={"max_retries": 3, "last_error": "Connection refused"},
)
```

**When to use:** Retry policies have been fully exhausted.

### DegradedServiceException

Raised when a service is running in a degraded state.

```python
raise DegradedServiceException(
    "Running without cache -- degraded performance expected",
    code="DEGRADED_MODE",
)
```

**HTTP Status:** 503 Service Unavailable

### NotImplementedException

Raised when a requested operation is not yet implemented.

```python
raise NotImplementedException(
    "Bulk order import is not yet available",
    code="NOT_IMPLEMENTED",
)
```

**HTTP Status:** 501 Not Implemented

---

## External Service Exceptions

External service exceptions represent failures in communication with third-party or
upstream services. They inherit from `InfrastructureException`.

### ExternalServiceException

Base class for external/third-party service failures.

```python
raise ExternalServiceException(
    "Failed to call shipping API",
    code="SHIPPING_API_ERROR",
)
```

### ThirdPartyServiceException

Raised when a third-party service returns an error.

```python
raise ThirdPartyServiceException(
    "Stripe returned error: card_declined",
    code="STRIPE_ERROR",
    context={"stripe_code": "card_declined"},
)
```

### BadGatewayException

Raised when an upstream service returns an invalid response.

```python
raise BadGatewayException(
    "Inventory service returned malformed response",
    code="BAD_GATEWAY",
)
```

**HTTP Status:** 502 Bad Gateway

### GatewayTimeoutException

Raised when an upstream service does not respond in time.

```python
raise GatewayTimeoutException(
    "Inventory service did not respond within 10s",
    code="GATEWAY_TIMEOUT",
    context={"upstream": "inventory-service", "timeout": 10},
)
```

**HTTP Status:** 504 Gateway Timeout

---

## HTTP Status Mapping

The global exception handler in `src/pyfly/web/adapters/starlette/errors.py` maps
exceptions to HTTP status codes. The mapping uses most-specific-first ordering:

| Exception                      | HTTP Status | HTTP Status Name           |
|-------------------------------|-------------|----------------------------|
| `ValidationException`          | 422         | Unprocessable Entity       |
| `ResourceNotFoundException`    | 404         | Not Found                  |
| `ConflictException`            | 409         | Conflict                   |
| `PreconditionFailedException`  | 412         | Precondition Failed        |
| `GoneException`                | 410         | Gone                       |
| `InvalidRequestException`      | 400         | Bad Request                |
| `LockedResourceException`      | 423         | Locked                     |
| `MethodNotAllowedException`    | 405         | Method Not Allowed         |
| `UnsupportedMediaTypeException` | 415        | Unsupported Media Type     |
| `PayloadTooLargeException`     | 413         | Payload Too Large          |
| `UnauthorizedException`        | 401         | Unauthorized               |
| `ForbiddenException`           | 403         | Forbidden                  |
| `SecurityException`            | 403         | Forbidden                  |
| `QuotaExceededException`       | 429         | Too Many Requests          |
| `RateLimitException`           | 429         | Too Many Requests          |
| `CircuitBreakerException`      | 503         | Service Unavailable        |
| `BulkheadException`            | 503         | Service Unavailable        |
| `ServiceUnavailableException`  | 503         | Service Unavailable        |
| `DegradedServiceException`     | 503         | Service Unavailable        |
| `OperationTimeoutException`    | 504         | Gateway Timeout            |
| `NotImplementedException`      | 501         | Not Implemented            |
| `BadGatewayException`          | 502         | Bad Gateway                |
| `GatewayTimeoutException`      | 504         | Gateway Timeout            |
| `BusinessException` (catch-all) | 400        | Bad Request                |
| `InfrastructureException` (catch-all) | 502  | Bad Gateway                |
| Non-`PyFlyException`          | 500         | Internal Server Error      |

For non-`PyFlyException` errors, the global handler **first runs the
`ExceptionConverterService`** (`pyfly.web.converters`) to translate known library
exceptions into PyFly exceptions before deciding the status:

| Library Exception                       | Converted To                | HTTP |
|-----------------------------------------|-----------------------------|------|
| `pydantic.ValidationError`              | `ValidationException`       | 422  |
| `json.JSONDecodeError`                  | `InvalidRequestException`   | 400  |
| `TimeoutError` / `asyncio.TimeoutError` | `OperationTimeoutException` | 504  |

User-registered `ExceptionConverter` beans are appended to this chain. If no converter
matches, the handler returns a generic `500` response without leaking internal details.

---

## ErrorResponse

`ErrorResponse` is an RFC 7807-inspired dataclass for building structured error
payloads. It provides comprehensive metadata for error tracking, classification,
and client-side handling.

```python
from pyfly.kernel import ErrorResponse, ErrorCategory, ErrorSeverity, FieldError
```

### Core Fields

These fields are always present in the serialized output:

| Field       | Type            | Description                           |
|-------------|-----------------|---------------------------------------|
| `timestamp` | `str`           | ISO 8601 timestamp of the error       |
| `status`    | `int`           | HTTP status code                      |
| `error`     | `str`           | HTTP status text (e.g. "Not Found")   |
| `message`   | `str`           | Human-readable error description      |
| `code`      | `str`           | Machine-readable error code           |
| `path`      | `str`           | Request path that triggered the error |
| `category`  | `ErrorCategory` | Error classification                  |
| `severity`  | `ErrorSeverity` | Error severity level                  |
| `retryable` | `bool`          | Whether the client should retry       |

### Optional Fields

These fields are included in `to_dict()` output only when non-`None` or non-empty:

| Field              | Type                | Default          | Description                     |
|--------------------|---------------------|------------------|---------------------------------|
| `trace_id`         | `str \| None`       | `None`           | Distributed trace ID            |
| `span_id`          | `str \| None`       | `None`           | Span ID                         |
| `transaction_id`   | `str \| None`       | `None`           | Transaction ID                  |
| `retry_after`      | `int \| None`       | `None`           | Seconds to wait before retry    |
| `field_errors`     | `list[FieldError]`  | `[]`             | Validation field errors         |
| `debug_info`       | `dict \| None`      | `None`           | Debug information               |
| `suggestion`       | `str \| None`       | `None`           | Suggested corrective action     |
| `documentation_url` | `str \| None`      | `None`           | Link to relevant documentation  |

### to_dict() Method

Serializes the `ErrorResponse` to a dictionary suitable for JSON responses:

```python
response = ErrorResponse(
    timestamp="2026-01-15T10:30:00Z",
    status=422,
    error="Unprocessable Entity",
    message="Validation failed",
    code="VALIDATION_ERROR",
    path="/api/orders",
    category=ErrorCategory.VALIDATION,
    severity=ErrorSeverity.LOW,
    retryable=False,
    field_errors=[
        FieldError(field="quantity", message="must be positive", rejected_value=-1),
        FieldError(field="email", message="invalid format", rejected_value="not-an-email"),
    ],
    suggestion="Check the field_errors array for details on each invalid field.",
)

json_dict = response.to_dict()
# {
#     "timestamp": "2026-01-15T10:30:00Z",
#     "status": 422,
#     "error": "Unprocessable Entity",
#     "message": "Validation failed",
#     "code": "VALIDATION_ERROR",
#     "path": "/api/orders",
#     "category": "VALIDATION",
#     "severity": "LOW",
#     "retryable": False,
#     "field_errors": [
#         {"field": "quantity", "message": "must be positive", "rejected_value": -1},
#         {"field": "email", "message": "invalid format", "rejected_value": "not-an-email"}
#     ],
#     "suggestion": "Check the field_errors array for details on each invalid field."
# }
```

**Source:** `src/pyfly/kernel/types.py`

---

## ErrorCategory Enum

Classifies errors by their origin or domain:

```python
from pyfly.kernel import ErrorCategory

class ErrorCategory(Enum):
    VALIDATION = "VALIDATION"          # Input validation failures
    BUSINESS = "BUSINESS"              # Business rule violations
    TECHNICAL = "TECHNICAL"            # Internal technical errors
    SECURITY = "SECURITY"              # Authentication/authorization failures
    EXTERNAL = "EXTERNAL"              # Third-party service failures
    RESOURCE = "RESOURCE"              # Resource access issues (not found, gone)
    RATE_LIMIT = "RATE_LIMIT"          # Rate limiting / quota exceeded
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"  # Circuit breaker open
```

| Category         | When to Use                                      |
|------------------|--------------------------------------------------|
| `VALIDATION`     | Input does not conform to the expected schema     |
| `BUSINESS`       | Domain rules prevent the operation                |
| `TECHNICAL`      | Internal errors (bugs, configuration issues)      |
| `SECURITY`       | Authentication or authorization failed            |
| `EXTERNAL`       | A third-party dependency failed                   |
| `RESOURCE`       | The target resource is missing or inaccessible    |
| `RATE_LIMIT`     | Request rate or quota limit exceeded              |
| `CIRCUIT_BREAKER` | Circuit breaker is preventing requests           |

---

## ErrorSeverity Enum

Indicates how severe an error is, useful for alerting and monitoring:

```python
from pyfly.kernel import ErrorSeverity

class ErrorSeverity(Enum):
    LOW = "LOW"            # Minor issues, informational
    MEDIUM = "MEDIUM"      # Standard errors, default severity
    HIGH = "HIGH"          # Significant errors requiring attention
    CRITICAL = "CRITICAL"  # System-threatening errors, page immediately
```

| Severity   | Typical Use                                    |
|------------|------------------------------------------------|
| `LOW`      | Validation errors, expected failures           |
| `MEDIUM`   | Business rule violations, not-found errors     |
| `HIGH`     | External service failures, security violations |
| `CRITICAL` | Database outages, data corruption              |

---

## FieldError Dataclass

`FieldError` describes a validation error on a single field. Used within
`ErrorResponse.field_errors` to provide detailed, per-field error information.

```python
from pyfly.kernel import FieldError

@dataclass(frozen=True)
class FieldError:
    field: str
    message: str
    rejected_value: Any = None
```

| Field            | Type    | Description                              |
|------------------|---------|------------------------------------------|
| `field`          | `str`   | The field name that failed validation    |
| `message`        | `str`   | Human-readable error message for the field |
| `rejected_value` | `Any`   | The value that was rejected (optional)   |

**Usage:**

```python
FieldError(field="quantity", message="must be positive", rejected_value=-1)
FieldError(field="email", message="invalid email format", rejected_value="not-email")
FieldError(field="name", message="required")  # rejected_value defaults to None
```

---

## Complete Example

The following example shows a comprehensive error handling strategy for an order
microservice.

```python
"""order_service/services.py"""

from datetime import UTC, datetime
from pyfly.container import service
from pyfly.kernel import (
    ResourceNotFoundException,
    ValidationException,
    ConflictException,
    ConcurrencyException,
    ForbiddenException,
    ServiceUnavailableException,
    ErrorResponse,
    ErrorCategory,
    ErrorSeverity,
    FieldError,
)


@service
class OrderService:
    """Order service demonstrating comprehensive error handling."""

    def __init__(self, order_repo, inventory_client, auth_service) -> None:
        self._repo = order_repo
        self._inventory = inventory_client
        self._auth = auth_service

    async def create_order(self, user_id: str, data: dict) -> dict:
        """Create an order with multi-layer error handling."""

        # 1. Authorization check
        if not await self._auth.can_create_orders(user_id):
            raise ForbiddenException(
                "User is not authorized to create orders",
                code="ORDER_CREATE_FORBIDDEN",
                context={"user_id": user_id},
            )

        # 2. Business validation
        if not data.get("items"):
            raise ValidationException(
                "Order must contain at least one item",
                code="EMPTY_ORDER",
                context={
                    "errors": [
                        {"loc": ["items"], "msg": "at least one item required"}
                    ]
                },
            )

        # 3. Duplicate check
        existing = await self._repo.find_by_idempotency_key(
            data.get("idempotency_key")
        )
        if existing:
            raise ConflictException(
                "Order with this idempotency key already exists",
                code="DUPLICATE_ORDER",
                context={"existing_order_id": existing["id"]},
            )

        # 4. Inventory check (external service)
        try:
            await self._inventory.reserve_items(data["items"])
        except Exception as e:
            raise ServiceUnavailableException(
                "Inventory service is unavailable",
                code="INVENTORY_UNAVAILABLE",
                context={"original_error": str(e)},
            )

        # 5. Create the order
        order = await self._repo.save({
            "customer_id": user_id,
            "items": data["items"],
            "status": "created",
        })
        return order

    async def update_order(self, order_id: str, version: int, data: dict) -> dict:
        """Update with optimistic locking."""
        order = await self._repo.find_by_id(order_id)
        if order is None:
            raise ResourceNotFoundException(
                f"Order {order_id} not found",
                code="ORDER_NOT_FOUND",
                context={"order_id": order_id},
            )

        if order["version"] != version:
            raise ConcurrencyException(
                "Order was modified by another process",
                code="VERSION_MISMATCH",
                context={
                    "expected_version": version,
                    "actual_version": order["version"],
                },
            )

        order.update(data)
        return await self._repo.save(order)


# =========================================================================
# Building ErrorResponse manually (for custom error endpoints)
# =========================================================================

def build_validation_error_response(
    path: str,
    field_errors: list[FieldError],
) -> ErrorResponse:
    """Build a structured validation error response."""
    return ErrorResponse(
        timestamp=datetime.now(UTC).isoformat(),
        status=422,
        error="Unprocessable Entity",
        message="One or more fields failed validation",
        code="VALIDATION_ERROR",
        path=path,
        category=ErrorCategory.VALIDATION,
        severity=ErrorSeverity.LOW,
        retryable=False,
        field_errors=field_errors,
        suggestion="Review the field_errors array and correct the invalid fields.",
    )


def build_rate_limit_error_response(
    path: str,
    retry_after: int,
) -> ErrorResponse:
    """Build a rate limit error response with retry guidance."""
    return ErrorResponse(
        timestamp=datetime.now(UTC).isoformat(),
        status=429,
        error="Too Many Requests",
        message="Rate limit exceeded",
        code="RATE_LIMIT",
        path=path,
        category=ErrorCategory.RATE_LIMIT,
        severity=ErrorSeverity.MEDIUM,
        retryable=True,
        retry_after=retry_after,
        suggestion=f"Retry after {retry_after} seconds.",
    )


# =========================================================================
# Usage example
# =========================================================================

# Build a validation error response
response = build_validation_error_response(
    path="/api/orders",
    field_errors=[
        FieldError("quantity", "must be positive", rejected_value=-1),
        FieldError("email", "invalid format", rejected_value="bad-email"),
    ],
)

# Serialize to JSON-friendly dict
json_body = response.to_dict()
# Returns a dict with all core fields plus field_errors and suggestion
```

The global exception handler automatically produces similar responses for any
`PyFlyException` thrown during request processing. The service code above only needs
to `raise` the appropriate exception -- the web layer handles serialization and HTTP
status code mapping automatically.
