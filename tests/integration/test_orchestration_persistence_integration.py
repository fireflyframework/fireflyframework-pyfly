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
"""Integration tests for durable orchestration persistence providers.

Exercises :class:`RedisPersistenceProvider` against a real Redis (testcontainers)
and :class:`SqlAlchemyPersistenceProvider` against a real Postgres (testcontainers).

Gated by ``@requires_docker``. Deselected from the fast suite (``-m integration``).
Run via:
    PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \\
        tests/integration/test_orchestration_persistence_integration.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pyfly.testing import requires_docker
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
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return ExecutionState(
        correlation_id=str(uuid.uuid4()),
        name="integration-test",
        pattern=pattern,
        status=status,
        started_at=now,
        updated_at=now,
        completed_at=now if completed else None,
        payload={
            "correlation_id": str(uuid.uuid4()),
            "name": "integration-test",
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
# RedisPersistenceProvider — real Redis
# ===========================================================================


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_save_find(redis_url: str) -> None:
    """Save → find round-trip against a real Redis instance."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=f"test:{uuid.uuid4().hex[:8]}:")
        state = _make_state()
        await provider.save(state)

        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status
        assert found.pattern == state.pattern
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_find_all(redis_url: str) -> None:
    """find_all enumerates all saved states for a given prefix."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    prefix = f"test:{uuid.uuid4().hex[:8]}:"
    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=prefix)
        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(s1)
        await provider.save(s2)

        all_states = await provider.find_all()
        ids = {s.correlation_id for s in all_states}
        assert s1.correlation_id in ids
        assert s2.correlation_id in ids

        running_only = await provider.find_all(status=ExecutionStatus.RUNNING)
        assert all(s.status == ExecutionStatus.RUNNING for s in running_only)
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_delete(redis_url: str) -> None:
    """delete removes the key from Redis."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    prefix = f"test:{uuid.uuid4().hex[:8]}:"
    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=prefix)
        state = _make_state()
        await provider.save(state)

        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None

        # Idempotent: deleting again returns False
        deleted_again = await provider.delete(state.correlation_id)
        assert deleted_again is False
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_is_healthy(redis_url: str) -> None:
    """is_healthy returns True against a live Redis server."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client)
        assert await provider.is_healthy() is True
    finally:
        await client.aclose()


# ===========================================================================
# SqlAlchemyPersistenceProvider — real Postgres
# ===========================================================================


@requires_docker
@pytest.mark.asyncio
async def test_sqlalchemy_persistence_save_find(pg_url: str) -> None:
    """Save → find round-trip against a real Postgres instance."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

    engine = create_async_engine(pg_url, echo=False)
    try:
        provider = SqlAlchemyPersistenceProvider(engine)
        await provider.initialize()

        state = _make_state()
        await provider.save(state)

        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_sqlalchemy_persistence_find_all(pg_url: str) -> None:
    """find_all with optional status filter against real Postgres."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

    engine = create_async_engine(pg_url, echo=False)
    try:
        provider = SqlAlchemyPersistenceProvider(engine)
        await provider.initialize()

        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        s3 = _make_state(status=ExecutionStatus.FAILED)
        await provider.save(s1)
        await provider.save(s2)
        await provider.save(s3)

        all_states = await provider.find_all()
        assert len(all_states) == 3

        running = await provider.find_all(status=ExecutionStatus.RUNNING)
        assert len(running) == 1
        assert running[0].correlation_id == s1.correlation_id
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_sqlalchemy_persistence_delete(pg_url: str) -> None:
    """delete removes the row from Postgres."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

    engine = create_async_engine(pg_url, echo=False)
    try:
        provider = SqlAlchemyPersistenceProvider(engine)
        await provider.initialize()

        state = _make_state()
        await provider.save(state)

        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_sqlalchemy_persistence_is_healthy(pg_url: str) -> None:
    """is_healthy returns True against a live Postgres instance."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider

    engine = create_async_engine(pg_url, echo=False)
    try:
        provider = SqlAlchemyPersistenceProvider(engine)
        assert await provider.is_healthy() is True
    finally:
        await engine.dispose()
