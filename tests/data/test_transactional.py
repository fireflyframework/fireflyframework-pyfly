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
"""Tests for DI-aware @transactional decorator with propagation/isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transactional import (
    Isolation,
    Propagation,
    _active_session_var,
    _patch_repositories,
    transactional,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_async_cm(enter_value: object = None) -> MagicMock:
    """Return a synchronous MagicMock that acts as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=enter_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session_factory() -> MagicMock:
    """Build a mock async_sessionmaker that yields a mock AsyncSession."""
    session = MagicMock()
    session.begin = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execution_options = MagicMock(return_value=session)

    session_cm = _make_async_cm(enter_value=session)

    factory = MagicMock()
    factory.return_value = session_cm
    return factory


class _Service:
    """Minimal service stub with _session_factory."""

    def __init__(self, factory: MagicMock) -> None:
        self._session_factory = factory

    @transactional()
    async def do_work(self) -> str:
        return "ok"

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def do_new(self) -> str:
        return "new"

    @transactional(propagation=Propagation.MANDATORY)
    async def do_mandatory(self) -> str:
        return "mandatory"

    @transactional(propagation=Propagation.NEVER)
    async def do_never(self) -> str:
        return "never"

    @transactional(propagation=Propagation.SUPPORTS)
    async def do_supports(self) -> str:
        return "supports"

    @transactional(propagation=Propagation.NOT_SUPPORTED)
    async def do_not_supported(self) -> str:
        return "not_supported"

    @transactional(isolation=Isolation.SERIALIZABLE)
    async def do_serializable(self) -> str:
        return "serializable"


class _NoFactoryService:
    @transactional()
    async def do_work(self) -> str:
        return "ok"


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


class TestPropagationEnum:
    def test_values(self) -> None:
        assert Propagation.REQUIRED.value == "REQUIRED"
        assert Propagation.REQUIRES_NEW.value == "REQUIRES_NEW"
        assert Propagation.SUPPORTS.value == "SUPPORTS"
        assert Propagation.NOT_SUPPORTED.value == "NOT_SUPPORTED"
        assert Propagation.NEVER.value == "NEVER"
        assert Propagation.MANDATORY.value == "MANDATORY"

    def test_member_count(self) -> None:
        assert len(Propagation) == 6


class TestIsolationEnum:
    def test_values(self) -> None:
        assert Isolation.DEFAULT.value == "DEFAULT"
        assert Isolation.READ_UNCOMMITTED.value == "READ UNCOMMITTED"
        assert Isolation.READ_COMMITTED.value == "READ COMMITTED"
        assert Isolation.REPEATABLE_READ.value == "REPEATABLE READ"
        assert Isolation.SERIALIZABLE.value == "SERIALIZABLE"

    def test_member_count(self) -> None:
        assert len(Isolation) == 5


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------


class TestDecoratorMetadata:
    def test_metadata_set_on_function(self) -> None:
        svc = _Service(_make_session_factory())
        # Access the bound method's underlying function
        assert getattr(svc.do_work, "__pyfly_transactional__", False) is True
        assert getattr(svc.do_work, "__pyfly_propagation__", None) is Propagation.REQUIRED
        assert getattr(svc.do_work, "__pyfly_isolation__", None) is Isolation.DEFAULT

    def test_requires_new_metadata(self) -> None:
        svc = _Service(_make_session_factory())
        assert getattr(svc.do_new, "__pyfly_propagation__", None) is Propagation.REQUIRES_NEW

    def test_serializable_metadata(self) -> None:
        svc = _Service(_make_session_factory())
        assert getattr(svc.do_serializable, "__pyfly_isolation__", None) is Isolation.SERIALIZABLE


# ---------------------------------------------------------------------------
# Propagation.REQUIRED
# ---------------------------------------------------------------------------


class TestRequired:
    @pytest.mark.asyncio
    async def test_opens_session_when_none_active(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        result = await svc.do_work()
        assert result == "ok"
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_existing_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        mock_session = AsyncMock()
        token = _active_session_var.set(mock_session)
        try:
            result = await svc.do_work()
            assert result == "ok"
            factory.assert_not_called()
        finally:
            _active_session_var.reset(token)


# ---------------------------------------------------------------------------
# Propagation.REQUIRES_NEW
# ---------------------------------------------------------------------------


class TestRequiresNew:
    @pytest.mark.asyncio
    async def test_always_opens_new_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        mock_session = AsyncMock()
        token = _active_session_var.set(mock_session)
        try:
            result = await svc.do_new()
            assert result == "new"
            factory.assert_called_once()
        finally:
            _active_session_var.reset(token)

    @pytest.mark.asyncio
    async def test_restores_previous_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        previous = AsyncMock()
        token = _active_session_var.set(previous)
        try:
            await svc.do_new()
            assert _active_session_var.get() is previous
        finally:
            _active_session_var.reset(token)


# ---------------------------------------------------------------------------
# Propagation.MANDATORY
# ---------------------------------------------------------------------------


class TestMandatory:
    @pytest.mark.asyncio
    async def test_raises_when_no_active_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        with pytest.raises(RuntimeError, match="MANDATORY"):
            await svc.do_mandatory()

    @pytest.mark.asyncio
    async def test_succeeds_when_session_exists(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        token = _active_session_var.set(AsyncMock())
        try:
            result = await svc.do_mandatory()
            assert result == "mandatory"
        finally:
            _active_session_var.reset(token)


# ---------------------------------------------------------------------------
# Propagation.NEVER
# ---------------------------------------------------------------------------


class TestNever:
    @pytest.mark.asyncio
    async def test_raises_when_session_exists(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        token = _active_session_var.set(AsyncMock())
        try:
            with pytest.raises(RuntimeError, match="NEVER"):
                await svc.do_never()
        finally:
            _active_session_var.reset(token)

    @pytest.mark.asyncio
    async def test_succeeds_when_no_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        result = await svc.do_never()
        assert result == "never"


# ---------------------------------------------------------------------------
# Propagation.SUPPORTS
# ---------------------------------------------------------------------------


class TestSupports:
    @pytest.mark.asyncio
    async def test_runs_with_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        token = _active_session_var.set(AsyncMock())
        try:
            result = await svc.do_supports()
            assert result == "supports"
        finally:
            _active_session_var.reset(token)

    @pytest.mark.asyncio
    async def test_runs_without_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        result = await svc.do_supports()
        assert result == "supports"


# ---------------------------------------------------------------------------
# Propagation.NOT_SUPPORTED
# ---------------------------------------------------------------------------


class TestNotSupported:
    @pytest.mark.asyncio
    async def test_clears_active_session_during_execution(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        captured: list[object] = []

        @transactional(propagation=Propagation.NOT_SUPPORTED)
        async def capture_session(self_arg: object) -> None:
            captured.append(_active_session_var.get())

        original = AsyncMock()
        token = _active_session_var.set(original)
        try:
            await capture_session(svc)
            assert captured[0] is None
            assert _active_session_var.get() is original
        finally:
            _active_session_var.reset(token)

    @pytest.mark.asyncio
    async def test_runs_without_existing_session(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        result = await svc.do_not_supported()
        assert result == "not_supported"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    @pytest.mark.asyncio
    async def test_default_does_not_set_execution_options(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        await svc.do_work()

        ctx = factory.return_value
        session = ctx.__aenter__.return_value
        session.execution_options.assert_not_called()

    @pytest.mark.asyncio
    async def test_serializable_sets_execution_options(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        await svc.do_serializable()

        ctx = factory.return_value
        session = ctx.__aenter__.return_value
        session.execution_options.assert_called_once_with(
            isolation_level="SERIALIZABLE",
        )


# ---------------------------------------------------------------------------
# Repository patching
# ---------------------------------------------------------------------------


class TestPatchRepositories:
    def test_patches_repository_session(self) -> None:
        repo = MagicMock(spec=Repository)
        repo._session = None

        class Holder:
            pass

        holder = Holder()
        holder.my_repo = repo  # type: ignore[attr-defined]

        new_session = AsyncMock()
        _patch_repositories(holder, new_session)
        assert repo._session is new_session

    def test_ignores_non_repository_attributes(self) -> None:
        class Holder:
            pass

        holder = Holder()
        holder.name = "test"  # type: ignore[attr-defined]
        holder.value = 42  # type: ignore[attr-defined]

        _patch_repositories(holder, AsyncMock())


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    @pytest.mark.asyncio
    async def test_no_session_factory_raises(self) -> None:
        # A service exposing neither a relational _session_factory nor a document _motor_client
        # gets the unified "no transaction manager" error (it names both expected attributes).
        svc = _NoFactoryService()
        with pytest.raises(RuntimeError, match="no transaction manager"):
            await svc.do_work()

    @pytest.mark.asyncio
    async def test_exception_propagates(self) -> None:
        factory = _make_session_factory()

        class FailService:
            def __init__(self) -> None:
                self._session_factory = factory

            @transactional()
            async def fail(self) -> None:
                raise ValueError("boom")

        svc = FailService()
        with pytest.raises(ValueError, match="boom"):
            await svc.fail()


# ---------------------------------------------------------------------------
# ContextVar is reset after execution
# ---------------------------------------------------------------------------


class TestContextVarCleanup:
    @pytest.mark.asyncio
    async def test_context_var_reset_after_success(self) -> None:
        factory = _make_session_factory()
        svc = _Service(factory)
        assert _active_session_var.get() is None
        await svc.do_work()
        assert _active_session_var.get() is None

    @pytest.mark.asyncio
    async def test_context_var_reset_after_failure(self) -> None:
        factory = _make_session_factory()

        class Svc:
            def __init__(self) -> None:
                self._session_factory = factory

            @transactional()
            async def fail(self) -> None:
                raise ValueError("fail")

        svc = Svc()
        assert _active_session_var.get() is None
        with pytest.raises(ValueError):
            await svc.fail()
        assert _active_session_var.get() is None
