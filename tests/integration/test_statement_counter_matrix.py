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
"""``pyfly.testing.StatementCounter`` counts what each driver really sends, on every relational lane (C161).

The repository suites never counted statements, so ``save()`` = INSERT + SELECT and ``save_all(n)`` =
n SELECTs went unnoticed (F11). The counter these suites now use is proven here on SQLite (aiosqlite),
PostgreSQL (asyncpg), MySQL and MariaDB (asyncmy): one entry per cursor execution, with its verb, the
executemany flag, and the commits and rollbacks SQLAlchemy performs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from pyfly.testing import StatementCounter
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import ContractLine, ContractParent


async def test_counts_each_statement_by_verb_in_order(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(ContractLine)
    engine = relational_backend.create_engine()

    with StatementCounter(engine) as counter:
        async with engine.begin() as conn:
            await conn.execute(insert(ContractLine).values(order_code="A", line_no=1, sku="x"))
            await conn.execute(select(ContractLine.sku).where(ContractLine.order_code == "A"))
            await conn.execute(update(ContractLine).values(quantity=5).where(ContractLine.order_code == "A"))
            await conn.execute(select(ContractLine.quantity))
            await conn.execute(delete(ContractLine).where(ContractLine.order_code == "A"))

    assert counter.verbs() == ["INSERT", "SELECT", "UPDATE", "SELECT", "DELETE"]
    assert counter.counts() == {"INSERT": 1, "SELECT": 2, "UPDATE": 1, "DELETE": 1}
    assert counter.count() == 5
    assert counter.count("select") == 2
    assert counter.commits == 1
    assert counter.rollbacks == 0
    assert "contract_line" in counter.statements[0].sql.lower()


async def test_an_executemany_batch_is_one_round_trip(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(ContractLine)
    engine = relational_backend.create_engine()
    rows = [{"order_code": "B", "line_no": n, "sku": f"sku-{n}"} for n in range(1, 21)]

    with StatementCounter(engine) as counter:
        async with engine.begin() as conn:
            await conn.execute(insert(ContractLine), rows)

    assert counter.counts() == {"INSERT": 1}
    assert counter.statements[0].executemany is True
    assert len(counter.statements[0].parameters) == 20


async def test_counts_every_session_on_the_engine_and_the_rollbacks(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(ContractParent)
    engine = relational_backend.create_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with StatementCounter(engine) as counter:
        async with factory() as first, first.begin():
            first.add(ContractParent(id=uuid.uuid4(), name="committed"))
        async with factory() as second:
            second.add(ContractParent(id=uuid.uuid4(), name="rolled back"))
            await second.flush()
            await second.rollback()

    assert counter.counts() == {"INSERT": 2}
    assert (counter.commits, counter.rollbacks) == (1, 1)
    async with engine.connect() as conn:
        names = (await conn.execute(select(ContractParent.name))).scalars().all()
    assert names == ["committed"]


async def test_stops_counting_on_exit_and_resets(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    counter = StatementCounter(engine)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))  # before start: not counted
        with counter:
            assert counter.active
            await conn.execute(text("SELECT 1"))
        assert not counter.active
        await conn.execute(text("SELECT 1"))  # after stop: not counted

    assert counter.counts() == {"SELECT": 1}
    counter.reset()
    assert (counter.count(), counter.commits, counter.rollbacks) == (0, 0, 0)


@pytest.mark.backends("sqlite-file")
async def test_counter_on_one_engine_ignores_another(relational_backend: RelationalBackend) -> None:
    watched = relational_backend.create_engine()
    other = relational_backend.create_engine()
    with StatementCounter(watched) as counter:
        async with other.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with watched.connect() as conn:
            await conn.execute(text("SELECT 2"))
    assert [statement.sql for statement in counter.statements] == ["SELECT 2"]
