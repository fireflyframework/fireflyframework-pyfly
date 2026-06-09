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
"""Unit tests for :mod:`pyfly.data.relational.metrics` (R2dbcMetrics parity).

No Docker required — uses an in-memory ``sqlite+aiosqlite:///:memory:`` engine
and a fake :class:`MetricsRecorder` that records every ``.labels()/.inc()/.observe()``
call so assertions can inspect recorded metrics.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.data.relational.metrics import SqlAlchemyQueryMetrics, _operation

# ---------------------------------------------------------------------------
# Fake MetricsRecorder + handles
# ---------------------------------------------------------------------------


class _FakeHandle:
    """A metric handle that records every .labels()/.inc()/.observe() call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def labels(self, **kwargs: Any) -> _FakeLabelledHandle:
        return _FakeLabelledHandle(self, kwargs)

    def inc(self, amount: float = 1.0) -> None:
        self.calls.append({"labels": {}, "op": "inc", "amount": amount})

    def observe(self, value: float) -> None:
        self.calls.append({"labels": {}, "op": "observe", "value": value})


class _FakeLabelledHandle:
    def __init__(self, parent: _FakeHandle, label_values: dict[str, Any]) -> None:
        self._parent = parent
        self._labels = label_values

    def inc(self, amount: float = 1.0) -> None:
        self._parent.calls.append({"labels": self._labels, "op": "inc", "amount": amount})

    def observe(self, value: float) -> None:
        self._parent.calls.append({"labels": self._labels, "op": "observe", "value": value})


class _FakeRecorder:
    """Fake MetricsRecorder that maps metric name -> _FakeHandle."""

    def __init__(self) -> None:
        self.handles: dict[str, _FakeHandle] = {}

    def _get(self, name: str) -> _FakeHandle:
        if name not in self.handles:
            self.handles[name] = _FakeHandle(name)
        return self.handles[name]

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> _FakeHandle:
        return self._get(name)

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> _FakeHandle:
        return self._get(name)

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> _FakeHandle:
        return self._get(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_calls(handle: _FakeHandle, op: str, operation_label: str) -> int:
    return sum(1 for c in handle.calls if c["op"] == op and c["labels"].get("operation") == operation_label)


# ---------------------------------------------------------------------------
# _operation() unit tests (no I/O)
# ---------------------------------------------------------------------------


def test_operation_select() -> None:
    assert _operation("SELECT id FROM users WHERE id = 1") == "SELECT"


def test_operation_insert() -> None:
    assert _operation("INSERT INTO users (name) VALUES ('alice')") == "INSERT"


def test_operation_update() -> None:
    assert _operation("UPDATE users SET name='bob' WHERE id=1") == "UPDATE"


def test_operation_delete() -> None:
    assert _operation("DELETE FROM users WHERE id=1") == "DELETE"


def test_operation_ddl_maps_to_other() -> None:
    assert _operation("CREATE TABLE foo (id INT)") == "OTHER"
    assert _operation("DROP TABLE foo") == "OTHER"
    assert _operation("ALTER TABLE foo ADD COLUMN bar TEXT") == "OTHER"


def test_operation_empty_maps_to_other() -> None:
    assert _operation("") == "OTHER"
    assert _operation("   ") == "OTHER"


def test_operation_lowercase_normalised() -> None:
    assert _operation("select 1") == "SELECT"
    assert _operation("  insert into foo values (1)") == "INSERT"


def test_operation_unknown_verb_maps_to_other() -> None:
    assert _operation("MERGE INTO target USING source") == "OTHER"
    assert _operation("CALL my_proc()") == "OTHER"
    assert _operation("PRAGMA table_info(foo)") == "OTHER"


# ---------------------------------------------------------------------------
# attach() idempotence
# ---------------------------------------------------------------------------


async def test_attach_is_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    qm = SqlAlchemyQueryMetrics(engine, recorder)
    qm.attach()
    qm.attach()  # second call must not double-register listeners
    qm.attach()  # third call too
    assert qm._attached is True

    # Execute a simple query and confirm count metric fired exactly once.
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    count_handle = recorder.handles["pyfly_db_queries_total"]
    select_incs = _count_calls(count_handle, "inc", "SELECT")
    assert select_incs == 1, f"Expected exactly 1 SELECT inc, got {select_incs}"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Integration: real queries via engine.begin() / connect()
# ---------------------------------------------------------------------------


async def test_query_count_increments_for_each_statement() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    SqlAlchemyQueryMetrics(engine, recorder).attach()

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("INSERT INTO items (name) VALUES ('alpha')"))
        await conn.execute(text("INSERT INTO items (name) VALUES ('beta')"))

    async with engine.connect() as conn:
        await conn.execute(text("SELECT * FROM items"))

    count_handle = recorder.handles["pyfly_db_queries_total"]
    assert _count_calls(count_handle, "inc", "INSERT") == 2
    assert _count_calls(count_handle, "inc", "SELECT") >= 1
    assert _count_calls(count_handle, "inc", "OTHER") >= 1  # CREATE TABLE -> OTHER
    await engine.dispose()


async def test_duration_histogram_observed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    SqlAlchemyQueryMetrics(engine, recorder).attach()

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    duration_handle = recorder.handles["pyfly_db_query_duration_seconds"]
    observed = [c for c in duration_handle.calls if c["op"] == "observe"]
    assert len(observed) >= 1, "Expected at least one histogram observation"
    # Duration must be a non-negative float
    for obs in observed:
        assert obs["value"] >= 0.0


async def test_error_counter_increments_on_bad_sql() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    SqlAlchemyQueryMetrics(engine, recorder).attach()

    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT * FROM nonexistent_table_xyz"))

    error_handle = recorder.handles["pyfly_db_query_errors_total"]
    assert len(error_handle.calls) >= 1, "Expected at least one error counter increment"
    await engine.dispose()


async def test_metrics_constructed_with_correct_names() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    SqlAlchemyQueryMetrics(engine, recorder)

    # Recorder must have been called for all three metric names during __init__
    assert "pyfly_db_query_duration_seconds" in recorder.handles
    assert "pyfly_db_queries_total" in recorder.handles
    assert "pyfly_db_query_errors_total" in recorder.handles
    await engine.dispose()


# ---------------------------------------------------------------------------
# Wiring: query_metrics bean absent when registry is None
# ---------------------------------------------------------------------------


async def test_query_metrics_bean_returns_none_without_registry() -> None:
    from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    cfg = RelationalAutoConfiguration()
    result = cfg.query_metrics(engine, registry=None)
    assert result is None
    await engine.dispose()


async def test_query_metrics_bean_returns_lifecycle_with_registry() -> None:
    from pyfly.data.relational.auto_configuration import QueryMetricsLifecycle, RelationalAutoConfiguration

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    recorder = _FakeRecorder()
    cfg = RelationalAutoConfiguration()
    result = cfg.query_metrics(engine, registry=recorder)  # type: ignore[arg-type]
    assert isinstance(result, QueryMetricsLifecycle)
    await result.start()
    # After start the adapter must be attached
    assert result._metrics._attached is True
    await result.stop()  # no-op, must not raise
    await engine.dispose()
