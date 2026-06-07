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
"""Postgres advisory-lock DistributedLock adapter (v26.06.66)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock
from pyfly.scheduling.lock import DistributedLock


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakeConn:
    def __init__(self, acquire: bool) -> None:
        self._acquire = acquire
        self.closed = False
        self.calls: list[str] = []

    async def execute(self, statement: Any, params: dict | None = None) -> _FakeResult:
        sql = str(statement)
        self.calls.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self._acquire)
        return _FakeResult(True)  # unlock

    async def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, acquire: bool = True) -> None:
        self._acquire = acquire
        self.conns: list[_FakeConn] = []

    async def connect(self) -> _FakeConn:
        conn = _FakeConn(self._acquire)
        self.conns.append(conn)
        return conn


def test_key_is_deterministic_signed_64bit() -> None:
    k = PostgresAdvisoryLock._key("job")
    assert PostgresAdvisoryLock._key("job") == k  # deterministic (not salted hash())
    assert -(2**63) <= k < 2**63
    assert PostgresAdvisoryLock._key("other-job") != k


@pytest.mark.asyncio
async def test_acquire_holds_connection_then_release_unlocks_and_closes() -> None:
    engine = _FakeEngine(acquire=True)
    lock = PostgresAdvisoryLock(lambda: engine)
    assert await lock.try_acquire("job", 30.0) is True
    assert len(engine.conns) == 1 and engine.conns[0].closed is False  # connection held

    await lock.release("job")
    assert engine.conns[0].closed is True
    assert any("pg_advisory_unlock" in c for c in engine.conns[0].calls)


@pytest.mark.asyncio
async def test_acquire_failure_does_not_leak_connection() -> None:
    engine = _FakeEngine(acquire=False)
    lock = PostgresAdvisoryLock(lambda: engine)
    assert await lock.try_acquire("job", 30.0) is False
    assert engine.conns[0].closed is True  # released immediately, not leaked


@pytest.mark.asyncio
async def test_release_of_unheld_lock_is_noop() -> None:
    lock = PostgresAdvisoryLock(lambda: _FakeEngine())
    await lock.release("never-acquired")  # must not raise


@pytest.mark.asyncio
async def test_satisfies_distributed_lock_protocol() -> None:
    assert isinstance(PostgresAdvisoryLock(lambda: _FakeEngine()), DistributedLock)


def test_provider_postgres_selection() -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration

    cfg = Config({"pyfly": {"scheduling": {"lock": {"provider": "postgres"}}}})
    lock = SchedulingAutoConfiguration().distributed_lock(cfg, Container())
    assert isinstance(lock, PostgresAdvisoryLock)
