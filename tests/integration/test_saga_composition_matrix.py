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
"""A saga composition on the SQL persistence provider, on sqlite-file and PostgreSQL.

- A layer whose failing saga is not its last one: every saga of the layer that completed is compensated (the
  compositor used to raise at the failed one before recording the sagas after it, which were never
  compensated).
- Every saga of a composition is persisted as its own execution: the sagas used to run under the
  composition's correlation id, the key of ``pyfly_orchestration_state``, so the concurrent sagas of a layer
  overwrote one row, and recovery saw one of them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.framework_schema import orchestration_state
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import transactional
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.composition.composition_builder import SagaCompositionBuilder
from pyfly.transactional.saga.composition.compositor import SagaCompositor
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

SAGAS = ("w4-a-reserve-stock", "w4-b-charge-card", "w4-c-book-courier")


class FulfilmentRow(Base):
    __tablename__ = "w4_fulfilment_ledger"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class FulfilmentRows(Repository[FulfilmentRow, int]):
    pass


@service
class Fulfilment:
    def __init__(self, rows: FulfilmentRows) -> None:
        self.rows = rows

    @transactional
    async def record(self, kind: str) -> None:
        await self.rows.save(FulfilmentRow(kind=kind))


class ChargeDeclinedError(Exception):
    """The failing saga's step."""


@saga(name="w4-a-reserve-stock")
class ReserveStock:
    def __init__(self, fulfilment: Fulfilment) -> None:
        self.fulfilment = fulfilment

    @saga_step(id="reserve", compensate="release")
    async def reserve(self) -> None:
        await self.fulfilment.record("RESERVE")

    async def release(self) -> None:
        await self.fulfilment.record("RELEASE")


@saga(name="w4-b-charge-card")
class ChargeCard:
    @saga_step(id="charge")
    async def charge(self) -> None:
        raise ChargeDeclinedError("the card was declined")


@saga(name="w4-c-book-courier")
class BookCourier:
    def __init__(self, fulfilment: Fulfilment) -> None:
        self.fulfilment = fulfilment

    @saga_step(id="book", compensate="cancel")
    async def book(self) -> None:
        await self.fulfilment.record("BOOK")

    async def cancel(self) -> None:
        await self.fulfilment.record("CANCEL")


@pytest.fixture
async def context(relational_backend: RelationalBackend) -> AsyncIterator[ApplicationContext]:
    await relational_backend.create_tables(FulfilmentRow)
    await relational_backend.create_tables(orchestration_state)  # ddl-auto none: as a migration would
    ctx = ApplicationContext(
        relational_backend.config(
            {"pyfly.transactional.enabled": "true", "pyfly.transactional.persistence.provider": "sqlalchemy"}
        )
    )
    for bean in (FulfilmentRows, Fulfilment, ReserveStock, ChargeCard, BookCourier):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield ctx
    finally:
        await ctx.stop()


async def _committed(backend: RelationalBackend, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(text(sql))).all()]
    finally:
        await engine.dispose()


async def test_every_completed_saga_of_the_failing_layer_is_compensated_and_persisted_on_its_own(
    context: ApplicationContext, relational_backend: RelationalBackend
) -> None:
    builder = SagaCompositionBuilder("w4-fulfilment")
    for name in SAGAS:  # one layer, which runs a, b (fails) and c
        builder = builder.saga(name).depends_on().add()

    result = await SagaCompositor(context.get_bean(SagaEngine)).execute(builder.build())

    assert result.error is not None and "w4-b-charge-card" in str(result.error)
    assert {"w4-a-reserve-stock", "w4-c-book-courier"} <= set(result.compensated_sagas)
    kinds = await _committed(relational_backend, "SELECT kind FROM w4_fulfilment_ledger")
    assert sorted(kind for (kind,) in kinds) == ["BOOK", "CANCEL", "RELEASE", "RESERVE"]

    states = await _committed(
        relational_backend, "SELECT execution_name, correlation_id FROM pyfly_orchestration_state"
    )
    assert sorted(name for name, _ in states) == sorted(SAGAS)  # one execution per saga, none overwritten
    assert all(str(correlation_id).startswith(result.correlation_id) for _, correlation_id in states)
    assert {name: result.saga_results[name].correlation_id for name in SAGAS} == dict(states)
