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
- A saga the composition compensates is persisted as failed, with its compensated steps: it used to stay
  ``COMPLETED``, as if its effects had stayed.
- A composition run inside a caller's unit of work that rolls back: the compensation of a saga whose steps
  committed on their own commits on its own too. It used to run in the caller's task and join the caller's
  unit, so the caller's rollback took the compensation with it while the saga's effects stayed. On SQLite,
  whose database has one writer, the saga's own step cannot take the write lock the caller's write unit
  holds: it fails with "database is locked" (as every saga step inside a write ``@transactional`` does there).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping

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
from pyfly.transactional.saga.composition.composition_context import CompositionContext
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


class CheckoutFailedError(Exception):
    """The caller's failure: its unit of work rolls back."""


@service
class Checkout:
    """Runs a composition inside a write unit of its own, and fails when the composition does."""

    def __init__(self, sagas: SagaEngine) -> None:
        self.sagas = sagas
        self.last: CompositionContext | None = None

    @transactional
    async def checkout(self) -> None:
        composition = (
            SagaCompositionBuilder("w4-checkout")
            .saga("w4-a-reserve-stock")
            .depends_on()
            .add()
            .saga("w4-b-charge-card")
            .depends_on("w4-a-reserve-stock")
            .add()
            .build()
        )
        self.last = await SagaCompositor(self.sagas).execute(composition)
        if self.last.error is not None:
            raise CheckoutFailedError(str(self.last.error)) from self.last.error


async def _start(backend: RelationalBackend, overrides: Mapping[str, str] | None = None) -> ApplicationContext:
    await backend.create_tables(FulfilmentRow)
    await backend.create_tables(orchestration_state)  # ddl-auto none: as a migration would
    ctx = ApplicationContext(
        backend.config(
            {
                "pyfly.transactional.enabled": "true",
                "pyfly.transactional.persistence.provider": "sqlalchemy",
                **(overrides or {}),
            }
        )
    )
    for bean in (FulfilmentRows, Fulfilment, ReserveStock, ChargeCard, BookCourier, Checkout):
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


@pytest.fixture
async def context(relational_backend: RelationalBackend) -> AsyncIterator[ApplicationContext]:
    ctx = await _start(relational_backend)
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


async def test_the_sagas_a_composition_compensates_are_persisted_as_failed_with_their_compensated_steps(
    context: ApplicationContext, relational_backend: RelationalBackend
) -> None:
    builder = SagaCompositionBuilder("w4-fulfilment")
    for name in SAGAS:  # one layer, which runs a, b (fails) and c
        builder = builder.saga(name).depends_on().add()

    result = await SagaCompositor(context.get_bean(SagaEngine)).execute(builder.build())

    assert result.error is not None
    rows = await _committed(relational_backend, "SELECT execution_name, status, payload FROM pyfly_orchestration_state")
    # The row's payload is the execution; the saga's port state is its own payload.
    states = {name: (status, json.loads(str(payload))["payload"]) for name, status, payload in rows}
    # Every saga's effects are undone: none of them stayed COMPLETED.
    assert {name: status for name, (status, _) in states.items()} == dict.fromkeys(SAGAS, "FAILED")
    assert states["w4-a-reserve-stock"][1]["steps"] == {"reserve": {"status": "COMPENSATED"}}
    assert states["w4-c-book-courier"][1]["steps"] == {"book": {"status": "COMPENSATED"}}
    assert states["w4-a-reserve-stock"][1]["successful"] is False


async def test_a_composition_in_a_caller_unit_that_rolls_back_keeps_the_compensation_of_a_committed_saga(
    relational_backend: RelationalBackend,
) -> None:
    # SQLite waits busy_timeout for the write lock the caller's unit holds: keep that wait short.
    ctx = await _start(relational_backend, {"pyfly.data.relational.sqlite.busy-timeout": "200"})
    checkout = ctx.get_bean(Checkout)
    try:
        with pytest.raises(CheckoutFailedError):
            await checkout.checkout()
    finally:
        await ctx.stop()

    kinds = sorted(kind for (kind,) in await _committed(relational_backend, "SELECT kind FROM w4_fulfilment_ledger"))
    assert checkout.last is not None
    reserved = checkout.last.saga_results["w4-a-reserve-stock"]
    if relational_backend.is_embedded:
        # One writer: the saga's own step waits for the caller's write lock, and gives up.
        assert not reserved.success
        assert "database is locked" in str(reserved.steps["reserve"].error)
        assert kinds == []
    else:
        # The saga committed on its own, so its compensation did too: the caller's rollback undid neither.
        assert reserved.success
        assert "w4-a-reserve-stock" in checkout.last.compensated_sagas
        assert kinds == ["RELEASE", "RESERVE"]
