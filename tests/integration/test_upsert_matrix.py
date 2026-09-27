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
"""The dialect upsert helpers on every relational lane (C069).

The framework's stores used ``INSERT ... ON CONFLICT`` in ``text()``: a syntax error on MySQL and MariaDB.
``pyfly.data.relational.upsert`` sends each dialect its native form, and the portable forms (plain
``UPDATE``/``INSERT`` in a savepoint, what SQL Server and Oracle get) are run here on every lane too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pyfly.data.relational.framework_schema import UtcTimestamp
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.relational.upsert import (
    insert_if_absent,
    portable_insert_if_absent,
    portable_upsert,
    upsert,
)
from pyfly.data.transaction import TransactionTemplate
from pyfly.testing import StatementCounter
from tests.support.backend_matrix import MARIADB, MYSQL, RelationalBackend

snapshots = Table(
    "wp10a_snapshots",
    MetaData(),
    Column("aggregate_id", String(64), primary_key=True),
    Column("sequence", Integer, nullable=False),
    Column("payload", String(64), nullable=False),
    Column("expires_at", UtcTimestamp(), nullable=True),
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

Upsert = Callable[..., Awaitable[None]]
InsertIfAbsent = Callable[..., Awaitable[bool]]


async def _engine(backend: RelationalBackend) -> AsyncEngine:
    await backend.create_tables(snapshots)
    return backend.create_engine()


async def _rows(engine: AsyncEngine) -> dict[str, tuple[int, str]]:
    async with engine.connect() as connection:
        result = await connection.execute(select(snapshots.c.aggregate_id, snapshots.c.sequence, snapshots.c.payload))
        return {row.aggregate_id: (row.sequence, row.payload) for row in result}


def _row(aggregate_id: str, sequence: int, payload: str, expires_at: datetime | None = None) -> dict[str, Any]:
    return {"aggregate_id": aggregate_id, "sequence": sequence, "payload": payload, "expires_at": expires_at}


@pytest.mark.parametrize("helper", [upsert, portable_upsert], ids=["native", "portable"])
async def test_upsert_inserts_then_updates_the_row_with_the_key(
    relational_backend: RelationalBackend, helper: Upsert
) -> None:
    engine = await _engine(relational_backend)

    async with engine.begin() as connection:
        await helper(connection, snapshots, _row("a", 1, "first"), key=["aggregate_id"])
    async with engine.begin() as connection:
        await helper(connection, snapshots, _row("a", 2, "second"), key=["aggregate_id"])
        await helper(connection, snapshots, _row("b", 1, "other"), key=["aggregate_id"])

    assert await _rows(engine) == {"a": (2, "second"), "b": (1, "other")}


@pytest.mark.parametrize("helper", [upsert, portable_upsert], ids=["native", "portable"])
async def test_upsert_updates_only_the_named_columns(relational_backend: RelationalBackend, helper: Upsert) -> None:
    engine = await _engine(relational_backend)
    async with engine.begin() as connection:
        await helper(connection, snapshots, _row("a", 1, "kept"), key=["aggregate_id"])
        await helper(connection, snapshots, _row("a", 9, "ignored"), key=["aggregate_id"], update=["sequence"])

    assert await _rows(engine) == {"a": (9, "kept")}


@pytest.mark.parametrize("helper", [upsert, portable_upsert], ids=["native", "portable"])
async def test_a_conditional_upsert_only_moves_forward(relational_backend: RelationalBackend, helper: Upsert) -> None:
    """The snapshot rule (only a newer sequence replaces the stored one), on the existing row and the incoming
    values: MySQL assigns ON DUPLICATE KEY UPDATE left to right, so the condition must not see its own writes."""
    engine = await _engine(relational_backend)

    def newer(existing: Any, incoming: Any) -> Any:
        return existing.sequence < incoming.sequence

    async with engine.begin() as connection:
        await helper(connection, snapshots, _row("a", 5, "five"), key=["aggregate_id"], where=newer)
        await helper(connection, snapshots, _row("a", 3, "three"), key=["aggregate_id"], where=newer)
    assert await _rows(engine) == {"a": (5, "five")}

    async with engine.begin() as connection:
        await helper(connection, snapshots, _row("a", 7, "seven"), key=["aggregate_id"], where=newer)
    assert await _rows(engine) == {"a": (7, "seven")}


@pytest.mark.parametrize("helper", [insert_if_absent, portable_insert_if_absent], ids=["native", "portable"])
async def test_insert_if_absent_writes_only_when_the_key_is_free(
    relational_backend: RelationalBackend, helper: InsertIfAbsent
) -> None:
    engine = await _engine(relational_backend)

    async with engine.begin() as connection:
        assert await helper(connection, snapshots, _row("a", 1, "first"), key=["aggregate_id"]) is True
        assert await helper(connection, snapshots, _row("a", 2, "second"), key=["aggregate_id"]) is False

    assert await _rows(engine) == {"a": (1, "first")}


@pytest.mark.parametrize("helper", [insert_if_absent, portable_insert_if_absent], ids=["native", "portable"])
async def test_insert_if_absent_replaces_a_row_the_condition_lets_go(
    relational_backend: RelationalBackend, helper: InsertIfAbsent
) -> None:
    """An expired entry is taken again (the put_if_absent defect, C092); a live one is not."""
    engine = await _engine(relational_backend)
    expired = snapshots.c.expires_at <= NOW
    async with engine.begin() as connection:
        await connection.execute(
            snapshots.insert(),
            [_row("expired", 1, "old", NOW - timedelta(seconds=1)), _row("live", 1, "old", NOW + timedelta(hours=1))],
        )

    async with engine.begin() as connection:
        took_expired = await helper(
            connection,
            snapshots,
            _row("expired", 2, "new", NOW + timedelta(hours=1)),
            key=["aggregate_id"],
            replace_where=expired,
        )
        took_live = await helper(
            connection,
            snapshots,
            _row("live", 2, "new", NOW + timedelta(hours=1)),
            key=["aggregate_id"],
            replace_where=expired,
        )
        took_free = await helper(
            connection, snapshots, _row("free", 2, "new"), key=["aggregate_id"], replace_where=expired
        )

    assert (took_expired, took_live, took_free) == (True, False, True)
    assert await _rows(engine) == {"expired": (2, "new"), "live": (1, "old"), "free": (2, "new")}


async def test_the_portable_insert_raises_an_integrity_failure_that_is_no_duplicate(
    relational_backend: RelationalBackend,
) -> None:
    engine = await _engine(relational_backend)
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await portable_insert_if_absent(connection, snapshots, _row("a", 1, None), key=["aggregate_id"])  # type: ignore[arg-type]


async def test_a_duplicate_in_a_unit_of_work_leaves_the_unit_able_to_commit(
    relational_backend: RelationalBackend,
) -> None:
    """The portable insert meets the duplicate key in a savepoint: the unit forgets the failure when the
    savepoint rolls back, and commits everything else (on PostgreSQL a failed statement would otherwise
    doom the whole transaction)."""
    engine = await _engine(relational_backend)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    async with template.transaction() as unit:
        assert unit is not None
        session = unit.resource
        assert await portable_insert_if_absent(session, snapshots, _row("a", 1, "first"), key=["aggregate_id"])
        assert not await portable_insert_if_absent(session, snapshots, _row("a", 2, "dup"), key=["aggregate_id"])
        await portable_upsert(session, snapshots, _row("b", 1, "after"), key=["aggregate_id"])

    assert await _rows(engine) == {"a": (1, "first"), "b": (1, "after")}


async def test_the_native_forms_are_one_statement(relational_backend: RelationalBackend) -> None:
    engine = await _engine(relational_backend)
    two_step_replace = relational_backend.lane in (MYSQL, MARIADB)  # take-over UPDATE, then INSERT IGNORE

    async def count(operation: Callable[[AsyncConnection], Awaitable[Any]]) -> dict[str, int]:
        with StatementCounter(engine) as counter:
            async with engine.begin() as connection:
                await operation(connection)
        return counter.counts()

    assert await count(lambda c: upsert(c, snapshots, _row("a", 1, "x"), key=["aggregate_id"])) == {"INSERT": 1}
    assert await count(lambda c: upsert(c, snapshots, _row("a", 2, "y"), key=["aggregate_id"])) == {"INSERT": 1}
    assert await count(lambda c: insert_if_absent(c, snapshots, _row("b", 1, "x"), key=["aggregate_id"])) == {
        "INSERT": 1
    }
    replacing = await count(
        lambda c: insert_if_absent(
            c, snapshots, _row("b", 2, "y"), key=["aggregate_id"], replace_where=snapshots.c.sequence < 5
        )
    )
    assert replacing == ({"UPDATE": 1} if two_step_replace else {"INSERT": 1})
    assert await _rows(engine) == {"a": (2, "y"), "b": (2, "y")}


@pytest.mark.parametrize("helper", [insert_if_absent, portable_insert_if_absent], ids=["native", "portable"])
async def test_concurrent_callers_for_one_key_let_exactly_one_write(
    relational_backend: RelationalBackend, helper: InsertIfAbsent
) -> None:
    """Each caller on a connection and a short transaction of its own, as a lock or a dedupe marker runs."""
    engine = await _engine(relational_backend)

    async def attempt(index: int) -> bool:
        async with engine.begin() as connection:
            return await helper(
                connection, snapshots, _row("contended", index, f"caller-{index}"), key=["aggregate_id"]
            )

    outcomes = await asyncio.gather(*(attempt(index) for index in range(8)))

    assert outcomes.count(True) == 1
    winner = outcomes.index(True)
    assert await _rows(engine) == {"contended": (winner, f"caller-{winner}")}
