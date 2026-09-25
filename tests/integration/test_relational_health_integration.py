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
"""The ``db`` readiness check against a real PostgreSQL that goes silent (C021, audit scenario 3).

A TCP proxy sits between the application and the server. Once the pool holds a connection, the proxy
stops forwarding in both directions: a network partition, or a server that froze. The check's
``SELECT 1`` then runs on an established asyncpg connection, and cancelling it makes asyncpg open a
second connection to send a cancel request, which the partition swallows too. The check must still
answer DOWN within its timeout, answer the next probe in time as well, and report UP once the network
is back.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import TypeVar

import pytest
from sqlalchemy.engine import make_url

from pyfly.actuator.health import HealthStatus
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from tests.support.backend_matrix import PG, RelationalBackend
from tests.support.partition_proxy import PartitionProxy

T = TypeVar("T")

_TIMEOUT = 0.5
_SLACK = 0.5


async def _answer_within(awaitable: Awaitable[T], seconds: float) -> tuple[T, float]:
    """The result of *awaitable* and how long it took; fails the test when it takes over *seconds*.

    It does not wait for a late awaitable to wind down (``asyncio.wait_for`` would, and a check stuck in
    its driver's cleanup would hang the test instead of failing it).
    """
    task = asyncio.ensure_future(awaitable)
    started = time.monotonic()
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        task.cancel()
        pytest.fail(f"no answer within {seconds:g} s")
    return task.result(), time.monotonic() - started


@pytest.mark.backends(PG)
async def test_database_silent_on_a_pooled_connection_answers_down_in_time(
    relational_backend: RelationalBackend,
) -> None:
    upstream = make_url(relational_backend.url)
    proxy = PartitionProxy(upstream.host or "127.0.0.1", int(upstream.port or 5432))
    port = await proxy.start()
    proxied = upstream.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    registry = DataSourceRegistry(relational_backend.config({"pyfly.data.relational.url": proxied}))
    engine = registry.primary.engine
    indicator = SqlAlchemyHealthIndicator(engine, registry=registry, timeout=_TIMEOUT)
    try:
        warm, _ = await _answer_within(indicator.health(), 10)
        assert warm.status == "UP"
        assert engine.pool.checkedin() == 1  # the next check runs on this established connection

        proxy.partition()
        first, elapsed = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
        assert first.status == "DOWN", first.details
        assert first.details["error"] == "TimeoutError"
        assert elapsed >= _TIMEOUT * 0.9

        # The first check is still stuck in the driver's cleanup; the next probe does not queue behind it
        # and does not borrow another connection.
        checked_out = engine.pool.checkedout()
        second, _ = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
        assert second.status == "DOWN", second.details
        assert second.details["error"] == "TimeoutError"
        assert "still running" in second.details["message"]
        assert engine.pool.checkedout() == checked_out

        proxy.heal()
        outcomes: list[HealthStatus] = []
        deadline = time.monotonic() + 15
        while not outcomes or outcomes[-1].status != "UP":
            assert time.monotonic() < deadline, [outcome.details for outcome in outcomes]
            outcome, _ = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
            outcomes.append(outcome)
            if outcome.status != "UP":
                await asyncio.sleep(0.1)
        assert engine.pool.checkedout() == 0
    finally:
        proxy.heal()
        await registry.close()
        await proxy.close()
