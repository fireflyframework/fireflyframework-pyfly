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
"""The damage the wildcard subscription did, named.

``PostgresEventBus._drain`` refuses to advance the group's cursor while no
handler is registered, so events published before a worker subscribed are not
lost. The CQRS cache bridge used to register a wildcard handler in every process
whether or not it had a rule, which made ``_handlers`` non-empty and defeated
that guard: an API process drained the worker's queue into a no-op and the jobs
stayed queued forever.
"""

from __future__ import annotations

import pytest

from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.eda_bridge import EdaCacheInvalidationBridge
from pyfly.eda.adapters.postgres import PostgresEventBus


def _bus_and_bridge() -> tuple[PostgresEventBus, EdaCacheInvalidationBridge]:
    bus = PostgresEventBus(dsn="postgresql://x/y", group="workers")
    return bus, EdaCacheInvalidationBridge(QueryCacheAdapter(cache=None))


@pytest.mark.asyncio
async def test_a_rule_less_bridge_leaves_the_bus_with_no_handlers() -> None:
    bus, bridge = _bus_and_bridge()
    bridge.subscribe(bus)
    assert bus._handlers == []

    # _drain() returns on the handler guard before it ever acquires a
    # connection — with no pool, reaching the pool would raise.
    assert bus._pool is None
    await bus._drain()


@pytest.mark.asyncio
async def test_a_bridge_with_a_rule_is_a_real_consumer() -> None:
    """The contrast: a bridge that has work to do does register, and does drain."""
    bus, bridge = _bus_and_bridge()
    bridge.register("order.updated", "order:{order_id}")
    bridge.subscribe(bus)
    assert [pattern for pattern, _ in bus._handlers] == ["order.updated"]

    with pytest.raises(AttributeError):
        await bus._drain()  # past the guard, into the (absent) pool
