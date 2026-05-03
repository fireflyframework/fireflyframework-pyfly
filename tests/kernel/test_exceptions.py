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
"""Tests for PyFly kernel exception hierarchy."""

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


class TestPyFlyException:
    def test_basic_creation(self):
        exc = PyFlyException("something went wrong")
        assert str(exc) == "something went wrong"
        assert exc.code is None
        assert exc.context == {}

    def test_with_error_code(self):
        exc = PyFlyException("bad request", code="VALIDATION_001")
        assert exc.code == "VALIDATION_001"

    def test_with_context(self):
        exc = PyFlyException("not found", code="NOT_FOUND", context={"entity": "Order", "id": "123"})
        assert exc.context["entity"] == "Order"
        assert exc.context["id"] == "123"

    def test_is_runtime_exception(self):
        exc = PyFlyException("test")
        assert isinstance(exc, Exception)

    def test_context_defaults_to_empty_dict(self):
        exc = PyFlyException("test")
        assert exc.context == {}
        # Ensure it's not shared between instances
        exc.context["key"] = "value"
        exc2 = PyFlyException("test2")
        assert exc2.context == {}


class TestExceptionHierarchy:
    def test_business_is_pyfly(self):
        assert issubclass(BusinessException, PyFlyException)

    def test_infrastructure_is_pyfly(self):
        assert issubclass(InfrastructureException, PyFlyException)

    def test_security_is_pyfly(self):
        assert issubclass(SecurityException, PyFlyException)

    def test_validation_is_business(self):
        assert issubclass(ValidationException, BusinessException)

    def test_not_found_is_business(self):
        assert issubclass(ResourceNotFoundException, BusinessException)

    def test_conflict_is_business(self):
        assert issubclass(ConflictException, BusinessException)

    def test_rate_limit_is_infrastructure(self):
        assert issubclass(RateLimitException, InfrastructureException)

    def test_circuit_breaker_is_infrastructure(self):
        assert issubclass(CircuitBreakerException, InfrastructureException)

    def test_service_unavailable_is_infrastructure(self):
        assert issubclass(ServiceUnavailableException, InfrastructureException)

    def test_catch_all_pyfly_exceptions(self):
        """Verify all exceptions can be caught with a single handler."""
        exceptions = [
            ValidationException("bad input"),
            ResourceNotFoundException("missing", code="NOT_FOUND"),
            RateLimitException("too fast"),
            SecurityException("unauthorized"),
            CircuitBreakerException("circuit open"),
        ]
        for exc in exceptions:
            try:
                raise exc
            except PyFlyException as caught:
                assert caught is exc


class TestExpandedBusinessExceptions:
    """Test that all new business exception types subclass BusinessException."""

    def test_precondition_failed_is_business(self):
        assert issubclass(PreconditionFailedException, BusinessException)

    def test_gone_is_business(self):
        assert issubclass(GoneException, BusinessException)

    def test_invalid_request_is_business(self):
        assert issubclass(InvalidRequestException, BusinessException)

    def test_data_integrity_is_business(self):
        assert issubclass(DataIntegrityException, BusinessException)

    def test_concurrency_is_business(self):
        assert issubclass(ConcurrencyException, BusinessException)

    def test_locked_resource_is_business(self):
        assert issubclass(LockedResourceException, BusinessException)

    def test_method_not_allowed_is_business(self):
        assert issubclass(MethodNotAllowedException, BusinessException)

    def test_unsupported_media_type_is_business(self):
        assert issubclass(UnsupportedMediaTypeException, BusinessException)

    def test_payload_too_large_is_business(self):
        assert issubclass(PayloadTooLargeException, BusinessException)

    def test_business_exceptions_are_pyfly(self):
        """All business exceptions should also be PyFlyException."""
        business_types = [
            PreconditionFailedException,
            GoneException,
            InvalidRequestException,
            DataIntegrityException,
            ConcurrencyException,
            LockedResourceException,
            MethodNotAllowedException,
            UnsupportedMediaTypeException,
            PayloadTooLargeException,
        ]
        for exc_type in business_types:
            assert issubclass(exc_type, PyFlyException), f"{exc_type.__name__} is not a PyFlyException"

    def test_business_exceptions_carry_code_and_context(self):
        """New business exceptions inherit PyFlyException __init__ behavior."""
        exc = PreconditionFailedException("precondition failed", code="PRE_001", context={"field": "version"})
        assert str(exc) == "precondition failed"
        assert exc.code == "PRE_001"
        assert exc.context == {"field": "version"}


class TestExpandedSecurityExceptions:
    """Test that all new security exception types subclass SecurityException."""

    def test_unauthorized_is_security(self):
        assert issubclass(UnauthorizedException, SecurityException)

    def test_forbidden_is_security(self):
        assert issubclass(ForbiddenException, SecurityException)

    def test_authorization_is_security(self):
        assert issubclass(AuthorizationException, SecurityException)

    def test_security_exceptions_are_pyfly(self):
        """All security exceptions should also be PyFlyException."""
        security_types = [UnauthorizedException, ForbiddenException, AuthorizationException]
        for exc_type in security_types:
            assert issubclass(exc_type, PyFlyException), f"{exc_type.__name__} is not a PyFlyException"

    def test_security_exceptions_carry_code_and_context(self):
        """New security exceptions inherit PyFlyException __init__ behavior."""
        exc = ForbiddenException("access denied", code="FORBIDDEN_001", context={"resource": "/admin"})
        assert str(exc) == "access denied"
        assert exc.code == "FORBIDDEN_001"
        assert exc.context == {"resource": "/admin"}


class TestExpandedInfrastructureExceptions:
    """Test that all new infrastructure exception types subclass InfrastructureException."""

    def test_bulkhead_is_infrastructure(self):
        assert issubclass(BulkheadException, InfrastructureException)

    def test_operation_timeout_is_infrastructure(self):
        assert issubclass(OperationTimeoutException, InfrastructureException)

    def test_retry_exhausted_is_infrastructure(self):
        assert issubclass(RetryExhaustedException, InfrastructureException)

    def test_degraded_service_is_infrastructure(self):
        assert issubclass(DegradedServiceException, InfrastructureException)

    def test_not_implemented_is_infrastructure(self):
        assert issubclass(NotImplementedException, InfrastructureException)

    def test_infrastructure_exceptions_are_pyfly(self):
        """All infrastructure exceptions should also be PyFlyException."""
        infra_types = [
            BulkheadException,
            OperationTimeoutException,
            RetryExhaustedException,
            DegradedServiceException,
            NotImplementedException,
        ]
        for exc_type in infra_types:
            assert issubclass(exc_type, PyFlyException), f"{exc_type.__name__} is not a PyFlyException"

    def test_infrastructure_exceptions_carry_code_and_context(self):
        """New infrastructure exceptions inherit PyFlyException __init__ behavior."""
        exc = OperationTimeoutException("timed out", code="TIMEOUT_001", context={"duration_ms": 5000})
        assert str(exc) == "timed out"
        assert exc.code == "TIMEOUT_001"
        assert exc.context == {"duration_ms": 5000}


class TestExpandedExternalServiceExceptions:
    """Test external service exception hierarchy."""

    def test_external_service_is_infrastructure(self):
        assert issubclass(ExternalServiceException, InfrastructureException)

    def test_third_party_service_is_external_service(self):
        assert issubclass(ThirdPartyServiceException, ExternalServiceException)

    def test_bad_gateway_is_external_service(self):
        assert issubclass(BadGatewayException, ExternalServiceException)

    def test_gateway_timeout_is_external_service(self):
        assert issubclass(GatewayTimeoutException, ExternalServiceException)

    def test_quota_exceeded_is_rate_limit(self):
        assert issubclass(QuotaExceededException, RateLimitException)

    def test_external_service_exceptions_are_pyfly(self):
        """All external service exceptions should also be PyFlyException."""
        external_types = [
            ExternalServiceException,
            ThirdPartyServiceException,
            BadGatewayException,
            GatewayTimeoutException,
            QuotaExceededException,
        ]
        for exc_type in external_types:
            assert issubclass(exc_type, PyFlyException), f"{exc_type.__name__} is not a PyFlyException"

    def test_external_service_is_also_infrastructure(self):
        """External service exceptions should be catchable as InfrastructureException."""
        exc = ThirdPartyServiceException("stripe down")
        assert isinstance(exc, InfrastructureException)

    def test_quota_exceeded_is_also_infrastructure(self):
        """QuotaExceededException inherits from RateLimitException which is InfrastructureException."""
        exc = QuotaExceededException("API quota exceeded", code="QUOTA_001")
        assert isinstance(exc, InfrastructureException)
        assert isinstance(exc, RateLimitException)


class TestErrorEnums:
    def test_error_category_values(self):
        from pyfly.kernel.types import ErrorCategory

        assert ErrorCategory.VALIDATION.value == "VALIDATION"
        assert ErrorCategory.BUSINESS.value == "BUSINESS"
        assert ErrorCategory.TECHNICAL.value == "TECHNICAL"
        assert ErrorCategory.SECURITY.value == "SECURITY"
        assert ErrorCategory.EXTERNAL.value == "EXTERNAL"
        assert ErrorCategory.RESOURCE.value == "RESOURCE"
        assert ErrorCategory.RATE_LIMIT.value == "RATE_LIMIT"
        assert ErrorCategory.CIRCUIT_BREAKER.value == "CIRCUIT_BREAKER"

    def test_error_severity_values(self):
        from pyfly.kernel.types import ErrorSeverity

        assert ErrorSeverity.LOW.value == "LOW"
        assert ErrorSeverity.MEDIUM.value == "MEDIUM"
        assert ErrorSeverity.HIGH.value == "HIGH"
        assert ErrorSeverity.CRITICAL.value == "CRITICAL"


class TestErrorResponse:
    def test_minimal_error_response(self):
        from pyfly.kernel.types import ErrorResponse

        resp = ErrorResponse(
            timestamp="2026-02-13T12:00:00Z",
            status=404,
            error="Not Found",
            message="Order not found",
            code="ORDER_NOT_FOUND",
            path="/api/orders/123",
        )
        assert resp.status == 404
        assert resp.code == "ORDER_NOT_FOUND"
        assert resp.retryable is False
        assert resp.trace_id is None

    def test_error_response_to_dict(self):
        from pyfly.kernel.types import ErrorCategory, ErrorResponse, ErrorSeverity

        resp = ErrorResponse(
            timestamp="2026-02-13T12:00:00Z",
            status=429,
            error="Too Many Requests",
            message="Rate limit exceeded",
            code="RATE_LIMIT",
            path="/api/orders",
            category=ErrorCategory.RATE_LIMIT,
            severity=ErrorSeverity.MEDIUM,
            retryable=True,
            retry_after=30,
        )
        d = resp.to_dict()
        assert d["status"] == 429
        assert d["category"] == "RATE_LIMIT"
        assert d["retryable"] is True
        assert d["retry_after"] == 30

    def test_error_response_with_validation_errors(self):
        from pyfly.kernel.types import ErrorResponse, FieldError

        field_errors = [
            FieldError(field="email", message="Invalid email format", rejected_value="not-email"),
            FieldError(field="age", message="Must be positive", rejected_value="-1"),
        ]
        resp = ErrorResponse(
            timestamp="2026-02-13T12:00:00Z",
            status=422,
            error="Validation Error",
            message="Input validation failed",
            code="VALIDATION_ERROR",
            path="/api/users",
            field_errors=field_errors,
        )
        assert len(resp.field_errors) == 2
        assert resp.field_errors[0].field == "email"
        d = resp.to_dict()
        assert len(d["field_errors"]) == 2
        assert d["field_errors"][0]["field"] == "email"

    def test_error_response_excludes_none_from_dict(self):
        from pyfly.kernel.types import ErrorResponse

        resp = ErrorResponse(
            timestamp="2026-02-13T12:00:00Z",
            status=500,
            error="Internal Server Error",
            message="Something went wrong",
            code="INTERNAL_ERROR",
            path="/api/test",
        )
        d = resp.to_dict()
        assert "trace_id" not in d
        assert "field_errors" not in d
        assert "retry_after" not in d
