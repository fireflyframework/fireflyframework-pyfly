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
"""A second boot of :class:`PostgresEventBus` issues no DDL.

The adapter used to replay ``CREATE TABLE/INDEX IF NOT EXISTS`` in every process
at every boot, so a serving role needed schema-creation rights — in practice
ownership of two framework-internal tables — for work it never did. These tests
drive ``start()`` against a fake asyncpg and assert on the statements it sends.
The privilege behaviour itself is proved against a real server in
``tests/integration/test_eda_postgres_least_privilege.py``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from pyfly.eda.adapters.postgres import PostgresEventBus


class FakeConnection:
    """Records every statement, and answers the existence probe as told."""

    def __init__(self, tables_present: bool) -> None:
        self.tables_present = tables_present
        self.executed: list[str] = []
        self.queried: list[str] = []
        self.listeners: list[str] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append(sql.strip())

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.queried.append(sql.strip())
        if "to_regclass" in sql:
            return self.tables_present
        if "RETURNING id" in sql:
            return 1
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []

    async def add_listener(self, channel: str, _callback: Any) -> None:
        self.listeners.append(channel)

    async def remove_listener(self, channel: str, _callback: Any) -> None:
        self.listeners.remove(channel)

    async def close(self) -> None:
        return None

    # -- assertions the tests read ------------------------------------

    @property
    def ddl(self) -> list[str]:
        return [sql for sql in self.executed if sql.upper().startswith("CREATE")]

    @property
    def probes(self) -> list[str]:
        return [sql for sql in self.queried if "to_regclass" in sql]


class _Acquired:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _Acquired:
        return _Acquired(self._conn)

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_asyncpg(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake ``asyncpg`` module; the connection is set per test."""
    holder: dict[str, FakeConnection] = {}

    async def create_pool(_dsn: str, **_kwargs: Any) -> FakePool:
        return FakePool(holder["conn"])

    async def connect(_dsn: str, **_kwargs: Any) -> FakeConnection:
        return holder["conn"]

    module = types.ModuleType("asyncpg")
    module.create_pool = create_pool  # type: ignore[attr-defined]
    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)

    def use(conn: FakeConnection) -> FakeConnection:
        holder["conn"] = conn
        return conn

    return use


OFFSETS_INSERT = "INSERT INTO pyfly_eda_offsets"


@pytest.mark.asyncio
async def test_a_second_boot_issues_no_ddl(fake_asyncpg: Any) -> None:
    conn = fake_asyncpg(FakeConnection(tables_present=True))
    bus = PostgresEventBus(dsn="postgresql://x/y", group="serving")
    try:
        await bus.start()
    finally:
        await bus.stop()

    assert conn.ddl == [], f"DDL was issued against existing tables: {conn.ddl}"
    assert conn.probes, "the adapter did not probe for the tables"
    assert any(sql.startswith(OFFSETS_INSERT) for sql in conn.executed), (
        "the consumer group's cursor row must still be created"
    )


@pytest.mark.asyncio
async def test_a_first_boot_still_creates_the_tables(fake_asyncpg: Any) -> None:
    conn = fake_asyncpg(FakeConnection(tables_present=False))
    bus = PostgresEventBus(dsn="postgresql://x/y")
    try:
        await bus.start()
    finally:
        await bus.stop()

    assert len(conn.ddl) == 2
    assert "CREATE TABLE IF NOT EXISTS pyfly_eda_outbox" in conn.ddl[0]
    assert "CREATE INDEX IF NOT EXISTS pyfly_eda_outbox_dest_idx" in conn.ddl[0]
    assert "CREATE TABLE IF NOT EXISTS pyfly_eda_offsets" in conn.ddl[1]
    assert any(sql.startswith(OFFSETS_INSERT) for sql in conn.executed)


@pytest.mark.asyncio
async def test_auto_create_tables_false_neither_probes_nor_creates(fake_asyncpg: Any) -> None:
    """Whatever the database holds: the framework issues no DDL at all."""
    conn = fake_asyncpg(FakeConnection(tables_present=False))
    bus = PostgresEventBus(dsn="postgresql://x/y", auto_create_tables=False)
    try:
        await bus.start()
    finally:
        await bus.stop()

    assert conn.ddl == []
    assert conn.probes == []
    assert any(sql.startswith(OFFSETS_INSERT) for sql in conn.executed)


@pytest.mark.asyncio
async def test_a_publisher_that_lazily_starts_hits_the_same_gate(fake_asyncpg: Any) -> None:
    """``publish()`` starts the bus on a bus nobody started — the DDL path too."""
    conn = fake_asyncpg(FakeConnection(tables_present=True))
    bus = PostgresEventBus(dsn="postgresql://x/y")
    try:
        await bus.publish("pyfly.events", "order.created", {"id": 1})
    finally:
        await bus.stop()

    assert conn.ddl == []
    assert any("INSERT INTO pyfly_eda_outbox" in sql for sql in conn.queried)


@pytest.mark.asyncio
async def test_the_probe_asks_about_both_tables(fake_asyncpg: Any) -> None:
    conn = fake_asyncpg(FakeConnection(tables_present=True))
    bus = PostgresEventBus(dsn="postgresql://x/y")
    try:
        await bus.start()
    finally:
        await bus.stop()

    probe = conn.probes[0]
    assert "pyfly_eda_outbox" in probe
    assert "pyfly_eda_offsets" in probe


def test_auto_create_tables_defaults_to_on() -> None:
    """A fresh database still just works — the flag is an opt-OUT."""
    assert PostgresEventBus(dsn="postgresql://x/y")._auto_create_tables is True
