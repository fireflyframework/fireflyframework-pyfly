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
"""Postgres SessionRegistry adapter (v26.06.68) — unit tests with a SQL-recording fake engine."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.concurrency import SessionRegistry


class _FakeResult:
    def __init__(self, rows: list | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self) -> list:
        return self._rows

    def scalar(self) -> Any:
        return self._scalar


class _FakeConn:
    def __init__(self, sql_log: list[str]) -> None:
        self._sql_log = sql_log

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, statement: Any, params: dict | None = None) -> _FakeResult:
        self._sql_log.append(str(statement))
        return _FakeResult()


class _FakeEngine:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def begin(self) -> _FakeConn:
        return _FakeConn(self.sql)

    def connect(self) -> _FakeConn:
        return _FakeConn(self.sql)


def test_rejects_invalid_table_name() -> None:
    with pytest.raises(ValueError, match="table name"):
        PostgresSessionRegistry(lambda: _FakeEngine(), table="bad; DROP TABLE users")


@pytest.mark.asyncio
async def test_satisfies_session_registry_protocol() -> None:
    assert isinstance(PostgresSessionRegistry(lambda: _FakeEngine()), SessionRegistry)


@pytest.mark.asyncio
async def test_register_issues_upsert_and_creates_table_once() -> None:
    engine = _FakeEngine()
    reg = PostgresSessionRegistry(lambda: engine)
    await reg.register("alice", "s1", 1.0)
    await reg.register("alice", "s2", 2.0)
    joined = " ".join(engine.sql)
    assert "CREATE TABLE IF NOT EXISTS" in joined
    assert joined.count("CREATE TABLE IF NOT EXISTS") == 1  # table ensured once, not per call
    assert "INSERT INTO" in joined and "ON CONFLICT (session_id)" in joined


@pytest.mark.asyncio
async def test_list_orders_by_created_at_and_count_uses_count() -> None:
    engine = _FakeEngine()
    reg = PostgresSessionRegistry(lambda: engine)
    await reg.list_sessions("alice")
    await reg.count("alice")
    joined = " ".join(engine.sql)
    assert "ORDER BY created_at ASC" in joined
    assert "SELECT COUNT(*)" in joined


@pytest.mark.asyncio
async def test_deregister_issues_delete() -> None:
    engine = _FakeEngine()
    reg = PostgresSessionRegistry(lambda: engine)
    await reg.deregister("alice", "s1")
    assert any("DELETE FROM" in s for s in engine.sql)


def test_provider_postgres_selection() -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.session.adapters.memory import InMemorySessionStore
    from pyfly.session.auto_configuration import SessionConcurrencyAutoConfiguration

    cfg = Config({"pyfly": {"session": {"concurrency": {"registry": "postgres"}}}})
    controller = SessionConcurrencyAutoConfiguration().session_concurrency_controller(
        cfg, InMemorySessionStore(), Container()
    )
    assert isinstance(controller._registry, PostgresSessionRegistry)
