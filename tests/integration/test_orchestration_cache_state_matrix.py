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
"""Cache-backed orchestration state follows the transaction-aware cache contract (WP13, WP14 follow-up).

``CachePersistenceProvider`` wrote to the cache at once wherever it was called, so a state saved inside a
business unit of work that then rolled back stayed cached (the SQL provider joins the unit and rolls back with
it). On a SQLite file and on PostgreSQL, inside real units of work:

- a save inside a unit that rolls back is dropped, and one inside a unit that commits is written at the
  commit (not seen before it); outside a unit it is written at once;
- a delete inside a unit waits for the commit too;
- the cache holds the state's JSON text: a live ORM entity a step returned is never cached as an object.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.transaction import TransactionTemplate
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import ExecutionState
from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class CachedStateEntity(Base):
    __tablename__ = "wp13_cached_state_entity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(32))


class RolledBackError(Exception):
    """The business failure the unit rolls back on."""


def _state(correlation_id: str, result: object = "ok") -> ExecutionState:
    now = datetime.now(UTC)
    return ExecutionState(
        correlation_id=correlation_id,
        name="order-saga",
        pattern=ExecutionPattern.SAGA,
        status=ExecutionStatus.RUNNING,
        started_at=now,
        updated_at=now,
        completed_at=None,
        payload={"steps": {"reserve": {"result": result}}},
    )


@pytest.fixture
async def context(relational_backend: RelationalBackend) -> AsyncIterator[ApplicationContext]:
    ctx = ApplicationContext(relational_backend.config())
    ctx.register_bean(RelationalAutoConfiguration)
    await ctx.start()
    try:
        yield ctx
    finally:
        await ctx.stop()


async def test_a_state_saved_in_a_unit_is_written_at_its_commit_and_dropped_at_its_rollback(
    context: ApplicationContext,
) -> None:
    provider = CachePersistenceProvider(InMemoryCache())
    template = TransactionTemplate()

    with pytest.raises(RolledBackError):
        async with template.transaction():
            await provider.save(_state("rolled-back"))
            raise RolledBackError
    assert await provider.find("rolled-back") is None

    async with template.transaction():
        await provider.save(_state("committed"))
        assert await provider.find("committed") is None  # not before the commit
    saved = await provider.find("committed")
    assert saved is not None and saved.status is ExecutionStatus.RUNNING

    await provider.save(_state("outside"))  # outside a unit: at once
    assert await provider.find("outside") is not None


async def test_a_delete_in_a_unit_waits_for_its_commit(context: ApplicationContext) -> None:
    provider = CachePersistenceProvider(InMemoryCache())
    await provider.save(_state("gone"))
    template = TransactionTemplate()

    with pytest.raises(RolledBackError):
        async with template.transaction():
            await provider.delete("gone")
            raise RolledBackError
    assert await provider.find("gone") is not None

    async with template.transaction():
        await provider.delete("gone")
    assert await provider.find("gone") is None


async def test_the_cache_holds_json_text_never_a_live_entity(context: ApplicationContext) -> None:
    cache = InMemoryCache()
    provider = CachePersistenceProvider(cache)
    entity = CachedStateEntity(id=1, name="reserved")

    await provider.save(_state("with-entity", result=entity))

    raw = await cache.with_namespace("orchestration").get("orchestration:with-entity")
    assert isinstance(raw, str)
    found = await provider.find("with-entity")
    assert found is not None and found.payload["steps"]["reserve"]["result"] == str(entity)
