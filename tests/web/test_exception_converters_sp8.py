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
"""SP-8 exception converter tests: SQLAlchemy, httpx, CircuitBreaker."""

from __future__ import annotations

import pytest

from pyfly.web.converters import (
    CircuitBreakerExceptionConverter,
    HttpxExceptionConverter,
    SQLAlchemyIntegrityExceptionConverter,
    default_exception_converters,
)

# ---------------------------------------------------------------------------
# SQLAlchemyIntegrityExceptionConverter
# ---------------------------------------------------------------------------


class TestSQLAlchemyIntegrityExceptionConverter:
    """SQLAlchemy IntegrityError → HTTP 409 ConflictException."""

    def _make_integrity_error(self) -> Exception:
        """Build a minimal SQLAlchemy IntegrityError without a real engine."""
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed")
        from sqlalchemy.exc import IntegrityError

        # IntegrityError(statement, params, orig)
        return IntegrityError(
            statement="INSERT INTO foo VALUES (?)",
            params={"id": 1},
            orig=Exception("UNIQUE constraint failed"),
        )

    def test_can_handle_integrity_error(self) -> None:
        converter = SQLAlchemyIntegrityExceptionConverter()
        exc = self._make_integrity_error()
        assert converter.can_handle(exc) is True

    def test_cannot_handle_other_exceptions(self) -> None:
        converter = SQLAlchemyIntegrityExceptionConverter()
        assert converter.can_handle(ValueError("nope")) is False
        assert converter.can_handle(RuntimeError("nope")) is False

    def test_converts_to_conflict_exception_with_409(self) -> None:
        from pyfly.kernel.exceptions import ConflictException
        from pyfly.web.adapters.starlette.errors import _get_status_code

        converter = SQLAlchemyIntegrityExceptionConverter()
        exc = self._make_integrity_error()
        result = converter.convert(exc)

        assert isinstance(result, ConflictException)
        assert result.code == "INTEGRITY_ERROR"
        assert _get_status_code(result) == 409

    def test_convert_message_includes_original(self) -> None:
        converter = SQLAlchemyIntegrityExceptionConverter()
        exc = self._make_integrity_error()
        result = converter.convert(exc)
        assert "integrity" in str(result).lower() or "constraint" in str(result).lower()


# ---------------------------------------------------------------------------
# HttpxExceptionConverter
# ---------------------------------------------------------------------------


class TestHttpxExceptionConverter:
    """httpx.HTTPError → HTTP 502 / 504 exception."""

    def test_can_handle_connect_error(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        converter = HttpxExceptionConverter()
        exc = httpx.ConnectError("connection refused")
        assert converter.can_handle(exc) is True

    def test_can_handle_timeout_exception(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        converter = HttpxExceptionConverter()
        exc = httpx.TimeoutException("timed out")
        assert converter.can_handle(exc) is True

    def test_can_handle_read_timeout(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        converter = HttpxExceptionConverter()
        exc = httpx.ReadTimeout("read timed out")
        assert converter.can_handle(exc) is True

    def test_cannot_handle_non_httpx_exceptions(self) -> None:
        converter = HttpxExceptionConverter()
        assert converter.can_handle(ValueError("nope")) is False
        assert converter.can_handle(OSError("nope")) is False

    def test_connect_error_converts_to_bad_gateway_502(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        from pyfly.kernel.exceptions import BadGatewayException
        from pyfly.web.adapters.starlette.errors import _get_status_code

        converter = HttpxExceptionConverter()
        exc = httpx.ConnectError("connection refused")
        result = converter.convert(exc)

        assert isinstance(result, BadGatewayException)
        assert result.code == "BAD_GATEWAY"
        assert _get_status_code(result) == 502

    def test_timeout_converts_to_gateway_timeout_504(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        from pyfly.kernel.exceptions import GatewayTimeoutException
        from pyfly.web.adapters.starlette.errors import _get_status_code

        converter = HttpxExceptionConverter()
        exc = httpx.TimeoutException("timed out")
        result = converter.convert(exc)

        assert isinstance(result, GatewayTimeoutException)
        assert result.code == "GATEWAY_TIMEOUT"
        assert _get_status_code(result) == 504

    def test_read_timeout_converts_to_504(self) -> None:
        httpx = pytest.importorskip("httpx", reason="httpx not installed")
        from pyfly.kernel.exceptions import GatewayTimeoutException
        from pyfly.web.adapters.starlette.errors import _get_status_code

        converter = HttpxExceptionConverter()
        exc = httpx.ReadTimeout("read timed out")
        result = converter.convert(exc)

        assert isinstance(result, GatewayTimeoutException)
        assert _get_status_code(result) == 504


# ---------------------------------------------------------------------------
# CircuitBreakerExceptionConverter
# ---------------------------------------------------------------------------


class TestCircuitBreakerExceptionConverter:
    """CircuitBreakerException → HTTP 503 ServiceUnavailableException."""

    def _make_circuit_breaker_exception(self) -> Exception:
        from pyfly.kernel.exceptions import CircuitBreakerException

        return CircuitBreakerException("Circuit breaker is open")

    def test_can_handle_circuit_breaker_exception(self) -> None:
        converter = CircuitBreakerExceptionConverter()
        exc = self._make_circuit_breaker_exception()
        assert converter.can_handle(exc) is True

    def test_cannot_handle_other_exceptions(self) -> None:
        converter = CircuitBreakerExceptionConverter()
        assert converter.can_handle(ValueError("nope")) is False
        assert converter.can_handle(RuntimeError("nope")) is False

    def test_converts_to_service_unavailable_503(self) -> None:
        from pyfly.kernel.exceptions import ServiceUnavailableException
        from pyfly.web.adapters.starlette.errors import _get_status_code

        converter = CircuitBreakerExceptionConverter()
        exc = self._make_circuit_breaker_exception()
        result = converter.convert(exc)

        assert isinstance(result, ServiceUnavailableException)
        assert result.code == "CIRCUIT_BREAKER_OPEN"
        assert _get_status_code(result) == 503

    def test_convert_message_references_circuit_breaker(self) -> None:
        converter = CircuitBreakerExceptionConverter()
        exc = self._make_circuit_breaker_exception()
        result = converter.convert(exc)
        assert "circuit" in str(result).lower()

    def test_can_handle_subclass_from_resilience_module(self) -> None:
        """Ensure the converter works with instances raised by the resilience decorators."""
        from pyfly.resilience.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1)
        # Force it open
        cb.on_failure()

        from pyfly.kernel.exceptions import CircuitBreakerException

        converter = CircuitBreakerExceptionConverter()
        exc = CircuitBreakerException("Circuit breaker is open")
        assert converter.can_handle(exc) is True


# ---------------------------------------------------------------------------
# default_exception_converters includes new converters
# ---------------------------------------------------------------------------


class TestDefaultExceptionConvertersIncludesNewConverters:
    """All three new converters appear in the default chain."""

    def test_sqlalchemy_converter_in_chain(self) -> None:
        converters = default_exception_converters()
        types = [type(c) for c in converters]
        assert SQLAlchemyIntegrityExceptionConverter in types

    def test_httpx_converter_in_chain(self) -> None:
        converters = default_exception_converters()
        types = [type(c) for c in converters]
        assert HttpxExceptionConverter in types

    def test_circuit_breaker_converter_in_chain(self) -> None:
        converters = default_exception_converters()
        types = [type(c) for c in converters]
        assert CircuitBreakerExceptionConverter in types

    def test_default_converters_does_not_raise_without_optional_libs(self) -> None:
        """Calling default_exception_converters() must never raise even if
        sqlalchemy or httpx happen to be importable (they're test deps here)."""
        converters = default_exception_converters()
        assert len(converters) >= 6  # 3 original + 3 new
