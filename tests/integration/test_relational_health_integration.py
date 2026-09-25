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
"""The ``db`` readiness check against a real database that goes silent (C021, audit scenario 3).

A TCP proxy sits between the application and the server. Once the pool holds a connection, the proxy
stops forwarding in both directions: a network partition, or a server that froze. The check's
``SELECT 1`` then runs on an established asyncpg connection, and cancelling it makes asyncpg open a
second connection to send a cancel request, which the partition swallows too. The check must still
answer DOWN within its timeout, give its pool slot back, answer the next probe in time as well, and
report UP once the network is back.

The same proxy also reproduces a middlebox that forgot an idle flow (a cloud NAT or load balancer after
its idle timeout): the pooled connection loses every byte while new connections reach the server. The
check that landed on it answers DOWN, closes that connection's socket instead of waiting for the kernel
to give up on it, and the next probe answers UP on a fresh connection. That case runs on PostgreSQL,
MySQL and MariaDB (whose lanes also turn pool pre-ping on).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import TypeVar, cast

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from pyfly.actuator.health import HealthStatus
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend
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


async def _slots_released(engine: AsyncEngine, *, within: float = 2.0) -> int:
    """The pool's checked-out count once it drops to 0, or the last count seen after *within* seconds."""
    pool = cast(QueuePool, engine.pool)
    deadline = time.monotonic() + within
    while pool.checkedout() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return pool.checkedout()


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

        # The late check's socket was closed, so it does not stay in the driver's cleanup: it gives its
        # pool slot back at once. The next probe tries a fresh connection, which the partition swallows
        # too, and answers DOWN in time as well; that attempt is dropped at the deadline, slot included.
        assert await _slots_released(engine) == 0
        second, elapsed = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
        assert second.status == "DOWN", second.details
        assert second.details["error"] == "TimeoutError"
        assert "still running" not in second.details["message"]
        assert elapsed >= _TIMEOUT * 0.9
        assert await _slots_released(engine) == 0

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


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_black_holed_pooled_connection_is_down_for_one_probe_only(
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

        # The middlebox forgets the pooled connection's flow; the database itself stays reachable.
        proxy.black_hole_established()
        first, elapsed = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
        assert first.status == "DOWN", first.details
        assert first.details["error"] == "TimeoutError"
        assert elapsed >= _TIMEOUT * 0.9

        # The late check's socket is closed rather than left to the kernel's retransmission timeout, so it
        # gives its pool slot back at once and the next probe finds the database healthy.
        assert await _slots_released(engine) == 0
        second, _ = await _answer_within(indicator.health(), _TIMEOUT + _SLACK)
        assert second.status == "UP", second.details
        assert engine.pool.checkedout() == 0
    finally:
        await registry.close()
        await proxy.close()


@pytest.mark.backends(PG)
async def test_a_cancelled_probe_does_not_stop_a_check_another_probe_waits_for(
    relational_backend: RelationalBackend,
) -> None:
    # A client that hangs up cancels its own probe; the readiness probe sharing the same check must still
    # get the check's real answer, not an early DOWN.
    upstream = make_url(relational_backend.url)
    proxy = PartitionProxy(upstream.host or "127.0.0.1", int(upstream.port or 5432))
    port = await proxy.start()
    proxied = upstream.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    registry = DataSourceRegistry(relational_backend.config({"pyfly.data.relational.url": proxied}))
    engine = registry.primary.engine
    indicator = SqlAlchemyHealthIndicator(engine, registry=registry, timeout=2.0)
    try:
        warm, _ = await _answer_within(indicator.health(), 10)
        assert warm.status == "UP"

        proxy.partition()  # the shared check's SELECT 1 waits on the wire
        hung_up = asyncio.ensure_future(indicator.health())
        readiness = asyncio.ensure_future(indicator.health())
        await asyncio.sleep(0.2)
        hung_up.cancel()
        await asyncio.sleep(0.1)
        proxy.heal()  # the held-back bytes arrive: the check answers well within its 2 s

        answer, _ = await _answer_within(readiness, 3)
        assert answer.status == "UP", answer.details
        assert hung_up.cancelled()
        assert await _slots_released(engine) == 0
    finally:
        proxy.heal()
        await registry.close()
        await proxy.close()
