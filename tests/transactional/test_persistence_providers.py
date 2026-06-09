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
"""Unit tests for the durable persistence providers.

* CachePersistenceProvider — backed by a real InMemoryCache.
* SqlAlchemyPersistenceProvider — backed by aiosqlite in-memory DB.
* RedisPersistenceProvider — backed by fakeredis if available, else skipped.
* Provider-selection auto-config — parametrized, mocked client/engine creation.

No Docker required; all tests run in the fast suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import ExecutionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    pattern: ExecutionPattern = ExecutionPattern.SAGA,
    minutes_ago: int = 0,
    completed: bool = False,
) -> ExecutionState:
    """Build a minimal :class:`ExecutionState` for tests."""
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return ExecutionState(
        correlation_id=str(uuid.uuid4()),
        name="test-orchestration",
        pattern=pattern,
        status=status,
        started_at=now,
        updated_at=now,
        completed_at=now if completed else None,
        payload={
            "correlation_id": str(uuid.uuid4()),
            "name": "test-orchestration",
            "pattern": pattern.value,
            "status": status.value,
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "completed_at": None,
            "input": None,
            "headers": {},
            "dry_run": False,
            "tcc_phase": None,
            "error": None,
            "steps": {},
            "variables": {},
            "idempotency_keys": [],
            "try_results": {},
        },
    )


# ===========================================================================
# CachePersistenceProvider (using real InMemoryCache)
# ===========================================================================


class TestCachePersistenceProvider:
    """CachePersistenceProvider backed by a real InMemoryCache.

    Key property: a SECOND CachePersistenceProvider sharing the SAME
    InMemoryCache must see saves made by the first — proving that the
    implementation does NOT maintain a private in-process index.
    """

    @pytest.fixture
    def cache(self) -> Any:
        from pyfly.cache.adapters.memory import InMemoryCache

        return InMemoryCache()

    @pytest.fixture
    def provider(self, cache: Any) -> Any:
        from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider

        return CachePersistenceProvider(cache)

    @pytest.fixture
    def provider2(self, cache: Any) -> Any:
        """Second provider sharing the same InMemoryCache — proves no private index."""
        from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider

        return CachePersistenceProvider(cache)

    async def test_save_and_find_roundtrip(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status
        assert found.pattern == state.pattern
        assert found.name == state.name

    async def test_find_returns_none_for_missing(self, provider: Any) -> None:
        result = await provider.find("nonexistent-id")
        assert result is None

    async def test_delete_returns_true_when_key_exists(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None

    async def test_delete_returns_false_when_key_missing(self, provider: Any) -> None:
        result = await provider.delete("never-saved")
        assert result is False

    async def test_find_all_returns_all_states(self, provider: Any) -> None:
        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(s1)
        await provider.save(s2)
        all_states = await provider.find_all()
        ids = {s.correlation_id for s in all_states}
        assert s1.correlation_id in ids
        assert s2.correlation_id in ids

    async def test_find_all_with_status_filter(self, provider: Any) -> None:
        running = _make_state(status=ExecutionStatus.RUNNING)
        completed = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(running)
        await provider.save(completed)
        running_only = await provider.find_all(status=ExecutionStatus.RUNNING)
        assert all(s.status == ExecutionStatus.RUNNING for s in running_only)
        assert running.correlation_id in {s.correlation_id for s in running_only}

    async def test_find_all_with_pattern_filter(self, provider: Any) -> None:
        saga = _make_state(pattern=ExecutionPattern.SAGA)
        workflow = _make_state(pattern=ExecutionPattern.WORKFLOW)
        await provider.save(saga)
        await provider.save(workflow)
        saga_only = await provider.find_all(pattern=ExecutionPattern.SAGA)
        assert all(s.pattern == ExecutionPattern.SAGA for s in saga_only)

    async def test_no_private_index_second_provider_sees_saves(self, provider: Any, provider2: Any) -> None:
        """Core durability proof: a second provider over the same cache must enumerate saves."""
        state = _make_state()
        await provider.save(state)  # saved via provider1

        # provider2 has NO knowledge of the save — it must find it from the cache key-space
        found = await provider2.find(state.correlation_id)
        assert found is not None, "Second provider must find a state saved by the first"

        all_states = await provider2.find_all()
        ids = {s.correlation_id for s in all_states}
        assert state.correlation_id in ids, (
            "find_all via second provider must enumerate from the cache backend, not from a private in-process index"
        )

    async def test_find_stale_returns_non_terminal_before_cutoff(self, provider: Any) -> None:
        old_running = _make_state(status=ExecutionStatus.RUNNING, minutes_ago=10)
        recent_running = _make_state(status=ExecutionStatus.RUNNING, minutes_ago=1)
        terminal = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=10, completed=True)
        await provider.save(old_running)
        await provider.save(recent_running)
        await provider.save(terminal)

        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        stale = await provider.find_stale(cutoff)
        stale_ids = {s.correlation_id for s in stale}
        assert old_running.correlation_id in stale_ids
        assert terminal.correlation_id not in stale_ids

    async def test_cleanup_removes_old_terminal_states(self, provider: Any) -> None:
        old_done = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=60, completed=True)
        recent_done = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=1, completed=True)
        running = _make_state(status=ExecutionStatus.RUNNING)
        await provider.save(old_done)
        await provider.save(recent_done)
        await provider.save(running)

        removed = await provider.cleanup(timedelta(minutes=30))
        assert removed == 1
        assert await provider.find(old_done.correlation_id) is None
        assert await provider.find(recent_done.correlation_id) is not None
        assert await provider.find(running.correlation_id) is not None

    async def test_is_healthy(self, provider: Any) -> None:
        assert await provider.is_healthy() is True

    async def test_overwrite_existing_state(self, provider: Any) -> None:
        state = _make_state(status=ExecutionStatus.RUNNING)
        await provider.save(state)

        updated = ExecutionState(
            correlation_id=state.correlation_id,
            name=state.name,
            pattern=state.pattern,
            status=ExecutionStatus.COMPLETED,
            started_at=state.started_at,
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            payload=state.payload,
        )
        await provider.save(updated)

        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.status == ExecutionStatus.COMPLETED


# ===========================================================================
# SqlAlchemyPersistenceProvider (using aiosqlite in-memory DB)
# ===========================================================================


class TestSqlAlchemyPersistenceProvider:
    """SqlAlchemyPersistenceProvider backed by an aiosqlite in-memory database."""

    @pytest.fixture
    async def provider(self) -> Any:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found]
        except ImportError:
            pytest.skip("sqlalchemy not installed")

        from pyfly.transactional.persistence.sqlalchemy_adapter import (
            SqlAlchemyPersistenceProvider,
        )

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        p = SqlAlchemyPersistenceProvider(engine)
        await p.initialize()
        return p

    async def test_save_and_find_roundtrip(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status

    async def test_find_returns_none_for_missing(self, provider: Any) -> None:
        result = await provider.find("nonexistent-id")
        assert result is None

    async def test_find_all_returns_saved_states(self, provider: Any) -> None:
        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(s1)
        await provider.save(s2)
        all_states = await provider.find_all()
        ids = {s.correlation_id for s in all_states}
        assert s1.correlation_id in ids
        assert s2.correlation_id in ids

    async def test_find_all_with_status_filter(self, provider: Any) -> None:
        running = _make_state(status=ExecutionStatus.RUNNING)
        completed = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(running)
        await provider.save(completed)
        running_only = await provider.find_all(status=ExecutionStatus.RUNNING)
        assert all(s.status == ExecutionStatus.RUNNING for s in running_only)

    async def test_delete(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None

    async def test_delete_missing_returns_false(self, provider: Any) -> None:
        result = await provider.delete("never-saved")
        assert result is False

    async def test_cleanup_removes_old_terminal(self, provider: Any) -> None:
        old_done = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=60, completed=True)
        recent_done = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=1, completed=True)
        running = _make_state(status=ExecutionStatus.RUNNING)
        await provider.save(old_done)
        await provider.save(recent_done)
        await provider.save(running)

        removed = await provider.cleanup(timedelta(minutes=30))
        assert removed >= 1
        assert await provider.find(old_done.correlation_id) is None
        assert await provider.find(running.correlation_id) is not None

    async def test_upsert_existing_record(self, provider: Any) -> None:
        state = _make_state(status=ExecutionStatus.RUNNING)
        await provider.save(state)

        updated = ExecutionState(
            correlation_id=state.correlation_id,
            name=state.name,
            pattern=state.pattern,
            status=ExecutionStatus.COMPLETED,
            started_at=state.started_at,
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            payload=state.payload,
        )
        await provider.save(updated)

        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.status == ExecutionStatus.COMPLETED

    async def test_is_healthy(self, provider: Any) -> None:
        assert await provider.is_healthy() is True


# ===========================================================================
# RedisPersistenceProvider (fakeredis if available, else skip)
# ===========================================================================


class TestRedisPersistenceProvider:
    """RedisPersistenceProvider backed by fakeredis (if installed), else skipped."""

    @pytest.fixture
    async def provider(self) -> Any:
        try:
            import fakeredis.aioredis as fakeredis  # type: ignore[import-not-found]
        except ImportError:
            pytest.skip("fakeredis not installed — install it to run this test suite locally")

        from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

        client = fakeredis.FakeRedis()
        return RedisPersistenceProvider(client)

    async def test_save_and_find_roundtrip(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status

    async def test_find_returns_none_for_missing(self, provider: Any) -> None:
        result = await provider.find("nonexistent")
        assert result is None

    async def test_find_all(self, provider: Any) -> None:
        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.FAILED)
        await provider.save(s1)
        await provider.save(s2)
        all_states = await provider.find_all()
        ids = {s.correlation_id for s in all_states}
        assert s1.correlation_id in ids
        assert s2.correlation_id in ids

    async def test_delete(self, provider: Any) -> None:
        state = _make_state()
        await provider.save(state)
        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None

    async def test_is_healthy(self, provider: Any) -> None:
        assert await provider.is_healthy() is True


# ===========================================================================
# Provider-selection auto-config unit tests (parametrized, no Docker)
# ===========================================================================


class TestOrchestrationPersistenceProviderSelection:
    """orchestration_persistence bean selects the correct provider type from config."""

    def _call_bean(self, config_dict: dict[str, Any], cache_adapter: Any = None) -> Any:
        """Instantiate TransactionalEngineAutoConfiguration and call the bean method."""
        from pyfly.core.config import Config
        from pyfly.transactional.auto_configuration import TransactionalEngineAutoConfiguration

        cfg = Config(config_dict)
        autoconfig = TransactionalEngineAutoConfiguration()
        return autoconfig.orchestration_persistence(cfg, cache_adapter)

    def test_default_returns_in_memory(self) -> None:
        from pyfly.transactional.core.persistence import InMemoryPersistenceProvider

        result = self._call_bean({})
        assert isinstance(result, InMemoryPersistenceProvider)

    def test_memory_returns_in_memory(self) -> None:
        from pyfly.transactional.core.persistence import InMemoryPersistenceProvider

        result = self._call_bean({"pyfly": {"transactional": {"persistence": {"provider": "memory"}}}})
        assert isinstance(result, InMemoryPersistenceProvider)

    def test_redis_returns_redis_provider(self) -> None:
        from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

        mock_client: MagicMock = MagicMock()
        with patch("redis.asyncio.from_url", return_value=mock_client):
            result = self._call_bean({"pyfly": {"transactional": {"persistence": {"provider": "redis"}}}})
        assert isinstance(result, RedisPersistenceProvider)

    def test_redis_unavailable_raises_value_error(self) -> None:
        with (
            patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=False),
            pytest.raises(ValueError, match="redis"),
        ):
            self._call_bean({"pyfly": {"transactional": {"persistence": {"provider": "redis"}}}})

    def test_sqlalchemy_returns_sqlalchemy_provider(self) -> None:
        from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

        mock_engine: MagicMock = MagicMock()
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=mock_engine):
            result = self._call_bean(
                {
                    "pyfly": {
                        "transactional": {
                            "persistence": {
                                "provider": "sqlalchemy",
                                "sqlalchemy": {"url": "sqlite+aiosqlite:///:memory:"},
                            }
                        }
                    }
                }
            )
        assert isinstance(result, SqlAlchemyPersistenceProvider)

    def test_sqlalchemy_falls_back_to_data_relational_url(self) -> None:
        from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

        mock_engine: MagicMock = MagicMock()
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=mock_engine):
            result = self._call_bean(
                {
                    "pyfly": {
                        "transactional": {"persistence": {"provider": "sqlalchemy"}},
                        "data": {"relational": {"url": "postgresql+asyncpg://localhost/test"}},
                    }
                }
            )
        assert isinstance(result, SqlAlchemyPersistenceProvider)

    def test_sqlalchemy_no_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="url"):
            self._call_bean({"pyfly": {"transactional": {"persistence": {"provider": "sqlalchemy"}}}})

    def test_sqlalchemy_unavailable_raises_value_error(self) -> None:
        with (
            patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=False),
            pytest.raises(ValueError, match="sqlalchemy"),
        ):
            self._call_bean(
                {
                    "pyfly": {
                        "transactional": {
                            "persistence": {
                                "provider": "sqlalchemy",
                                "sqlalchemy": {"url": "sqlite+aiosqlite:///:memory:"},
                            }
                        }
                    }
                }
            )

    def test_cache_returns_cache_provider(self) -> None:
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider

        cache = InMemoryCache()
        result = self._call_bean(
            {"pyfly": {"transactional": {"persistence": {"provider": "cache"}}}},
            cache_adapter=cache,
        )
        assert isinstance(result, CachePersistenceProvider)

    def test_cache_without_adapter_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="CacheAdapter"):
            self._call_bean(
                {"pyfly": {"transactional": {"persistence": {"provider": "cache"}}}},
                cache_adapter=None,
            )
