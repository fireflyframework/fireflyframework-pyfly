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
"""SP-8 exception converter tests: SQLAlchemy, persistence, httpx, CircuitBreaker.

The persistence converters answer 409 with the kernel's backend-neutral exceptions, and never put the SQL
statement or its bound values in the response (C158, C159): the IntegrityError converter used to return
``str(IntegrityError)``, with ``[SQL: ...]`` and ``[parameters: ...]``, as the 409 message.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import String, insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.data.exception_translation import translate_exception
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.kernel.exceptions import (
    ConcurrencyException,
    ConflictException,
    DataIntegrityException,
    DuplicateKeyException,
    OptimisticLockingFailureException,
)
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.converters import (
    CircuitBreakerExceptionConverter,
    HttpxExceptionConverter,
    PersistenceExceptionConverter,
    SQLAlchemyIntegrityExceptionConverter,
    default_exception_converters,
)

_SECRET_EMAIL = "private.person@example.com"
_SECRET_HASH = "$argon2id$SECRET-HASH-123"


class ConverterUser(BaseEntity):
    __tablename__ = "sp8_users"

    email: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))


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

    def test_convert_message_carries_no_sql_or_values(self) -> None:
        converter = SQLAlchemyIntegrityExceptionConverter()
        exc = self._make_integrity_error()
        result = converter.convert(exc)
        assert "integrity" in str(result).lower() or "constraint" in str(result).lower()
        assert "INSERT" not in str(result) and "{'id': 1}" not in str(result)
        assert isinstance(result, DataIntegrityException)
        assert result.__cause__ is exc


class TestPersistenceExceptionHierarchy:
    """The kernel's persistence exceptions are conflicts: HTTP 409, whoever raises them."""

    def test_hierarchy(self) -> None:
        assert issubclass(DuplicateKeyException, DataIntegrityException)
        assert issubclass(DataIntegrityException, ConflictException)
        assert issubclass(OptimisticLockingFailureException, ConcurrencyException)
        assert issubclass(ConcurrencyException, ConflictException)

    @pytest.mark.parametrize(
        "exc",
        [
            DataIntegrityException("fk", code="FK_VIOLATION"),
            ConcurrencyException("modified by another process", code="VERSION_MISMATCH"),
            DuplicateKeyException("dup"),
            OptimisticLockingFailureException("stale"),
        ],
    )
    def test_an_application_raised_one_is_a_409(self, exc: Exception) -> None:
        """A user-raised ConcurrencyException or DataIntegrityException used to answer 400."""

        async def boom(request: Request) -> JSONResponse:
            raise exc

        client = TestClient(create_app(extra_routes=[Route("/boom", boom)]), raise_server_exceptions=False)
        assert client.get("/boom").status_code == 409


class TestPersistenceExceptionConverter:
    def test_stale_data_error_is_an_optimistic_locking_failure(self) -> None:
        converter = PersistenceExceptionConverter()
        stale = StaleDataError("UPDATE statement on table 'sp8_users' expected to update 1 row(s); 0 were matched.")
        assert converter.can_handle(stale) is True
        result = converter.convert(stale)
        assert isinstance(result, OptimisticLockingFailureException)
        assert result.code == "OPTIMISTIC_LOCKING_FAILURE"
        assert "sp8_users" not in str(result)

    def test_other_exceptions_are_not_its_business(self) -> None:
        converter = PersistenceExceptionConverter()
        assert converter.can_handle(ValueError("nope")) is False
        assert converter.can_handle(DataIntegrityException("already translated")) is False

    def test_in_the_default_chain(self) -> None:
        assert PersistenceExceptionConverter in [type(c) for c in default_exception_converters()]

    def test_a_conversion_is_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """``can_handle`` asks whether a translation exists without translating; ``convert`` translates and
        logs the driver's message once."""
        converter = PersistenceExceptionConverter()
        stale = StaleDataError("UPDATE statement on table 'sp8_users' expected to update 1 row(s); 0 were matched.")
        with caplog.at_level(logging.DEBUG, logger="pyfly.data.exception_translation"):
            assert converter.can_handle(stale)
            converter.convert(stale)
        assert [record.getMessage() for record in caplog.records] == ["persistence_exception_translated"]


class _PyMssqlIntegrityError(Exception):
    """What pymssql raises: ``args[0]`` is an int error number too, and not a MySQL one."""


_PyMssqlIntegrityError.__module__ = "pymssql._pymssql"


class _AsyncmyOperationalError(Exception):
    """What asyncmy raises for a MySQL CHECK violation."""


_AsyncmyOperationalError.__module__ = "asyncmy.errors"


class TestMysqlErrorNumbersNeedAMysqlDriver:
    def test_another_driver_s_error_number_is_not_read_as_mysql(self) -> None:
        # 1062 is MySQL's ER_DUP_ENTRY; for SQL Server it is something else entirely.
        orig = _PyMssqlIntegrityError(1062, b"Some SQL Server error")
        translated = translate_exception(IntegrityError("INSERT ...", {}, orig))
        assert type(translated) is DataIntegrityException
        assert translated.context == {}

        operational = OperationalError("UPDATE ...", {}, _PyMssqlIntegrityError(1020, b"Some other error"))
        assert translate_exception(operational) is operational

    def test_a_mysql_driver_s_error_number_is(self) -> None:
        orig = _AsyncmyOperationalError(3819, "Check constraint 'ck_accounts_balance' is violated.")
        translated = translate_exception(OperationalError("UPDATE ...", {}, orig))
        assert isinstance(translated, DataIntegrityException)
        assert translated.context == {"violation": "check", "constraint": "ck_accounts_balance"}


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sp8.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[ConverterUser.__table__])
    yield engine
    await engine.dispose()


def _duplicate_routes(engine: AsyncEngine) -> list[Route]:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def through_repository(request: Request) -> JSONResponse:
        async with factory() as session:
            users: Repository[ConverterUser, uuid.UUID] = Repository(ConverterUser, session)
            await users.save(ConverterUser(email=_SECRET_EMAIL, password_hash=_SECRET_HASH))
            await session.commit()
            await users.save(ConverterUser(email=_SECRET_EMAIL, password_hash=_SECRET_HASH))
        return JSONResponse({})

    async def raw_statement(request: Request) -> JSONResponse:
        async with factory() as session:
            row = {"id": uuid.uuid4(), "email": _SECRET_EMAIL, "password_hash": _SECRET_HASH}
            await session.execute(insert(ConverterUser), [row])
            await session.execute(insert(ConverterUser), [{**row, "id": uuid.uuid4()}])
        return JSONResponse({})

    return [Route("/repository", through_repository, methods=["POST"]), Route("/raw", raw_statement, methods=["POST"])]


def _assert_no_leak(text: str) -> None:
    for leak in ("INSERT", "[SQL", "[parameters", _SECRET_EMAIL, _SECRET_HASH, "sqlite3", "sqlp8"):
        assert leak not in text, (leak, text)


class TestConflictResponsesCarryNoSql:
    """A duplicate insert answers 409 with the constraint's name, and neither the statement nor any value."""

    @pytest.mark.parametrize("path", ["/repository", "/raw"])
    @pytest.mark.parametrize("problem_details", [False, True], ids=["envelope", "problem-details"])
    async def test_duplicate_insert(self, engine: AsyncEngine, path: str, problem_details: bool) -> None:
        app = create_app(extra_routes=_duplicate_routes(engine))
        app.state.pyfly_problem_details = problem_details
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(path)

        assert response.status_code == 409
        _assert_no_leak(response.text)
        body: dict[str, Any] = response.json() if problem_details else response.json()["error"]
        assert body["code"] == "INTEGRITY_ERROR"
        assert body["context"] == {"violation": "unique", "constraint": "uq_sp8_users_email"}
        message = body["detail"] if problem_details else body["message"]
        assert message == "Duplicate key: unique constraint 'uq_sp8_users_email' violated"

    async def test_the_translated_exception_keeps_the_driver_error_for_the_logs(self, engine: AsyncEngine) -> None:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            users: Repository[ConverterUser, uuid.UUID] = Repository(ConverterUser, session)
            await users.save(ConverterUser(email=_SECRET_EMAIL, password_hash="x"))
            with pytest.raises(DuplicateKeyException) as raised:
                await users.save(ConverterUser(email=_SECRET_EMAIL, password_hash="y"))
        assert isinstance(raised.value.__cause__, IntegrityError)
        assert "[SQL: INSERT" in str(raised.value.__cause__)


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
