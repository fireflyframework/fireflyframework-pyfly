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
"""The cache-invalidation bridge consumes only what it has a rule for.

It used to subscribe ``"*"`` from every process the moment a CQRS context had an
EDA bus, whether or not any rule was registered — so a process subscribed to
everything in order to discard it, and on a durable bus that made it a consuming
member of the group.
"""

from __future__ import annotations

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.container.container import Container
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.core.config import Config
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.eda_bridge import EdaCacheInvalidationBridge
from pyfly.cqrs.config.auto_configuration import CqrsAutoConfiguration
from pyfly.cqrs.config.properties import CqrsProperties
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.types import EventEnvelope


class RecordingPublisher:
    """An ``EventPublisher`` that only remembers what was subscribed to it."""

    def __init__(self) -> None:
        self.subscriptions: list[str] = []

    def subscribe(self, event_type_pattern: str, handler: object) -> None:
        self.subscriptions.append(event_type_pattern)

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        return None


def bridge() -> EdaCacheInvalidationBridge:
    return EdaCacheInvalidationBridge(QueryCacheAdapter(cache=None))


class TestSubscriptions:
    def test_a_bridge_with_no_rule_subscribes_to_nothing(self) -> None:
        publisher = RecordingPublisher()
        bridge().subscribe(publisher)
        assert publisher.subscriptions == []

    def test_a_registered_rule_subscribes_that_event_type_and_not_the_wildcard(self) -> None:
        publisher = RecordingPublisher()
        b = bridge()
        b.register("order.updated", "order:{order_id}")
        b.subscribe(publisher)
        assert publisher.subscriptions == ["order.updated"]

    def test_a_rule_registered_after_attaching_subscribes_itself(self) -> None:
        publisher = RecordingPublisher()
        b = bridge()
        b.subscribe(publisher)
        assert publisher.subscriptions == []
        b.register("order.updated", "order:{order_id}")
        assert publisher.subscriptions == ["order.updated"]

    def test_a_second_event_type_adds_a_second_subscription(self) -> None:
        publisher = RecordingPublisher()
        b = bridge()
        b.register("order.updated", "order:{order_id}")
        b.register("order.deleted", "order:{order_id}")
        b.subscribe(publisher)
        assert publisher.subscriptions == ["order.updated", "order.deleted"]

    def test_two_patterns_for_one_event_type_subscribe_once(self) -> None:
        publisher = RecordingPublisher()
        b = bridge()
        b.register("order.updated", "order:{order_id}")
        b.register("order.updated", "orders:list")
        b.subscribe(publisher)
        assert publisher.subscriptions == ["order.updated"]


@pytest.mark.asyncio
async def test_a_narrow_subscription_still_evicts_over_a_real_bus() -> None:
    """The behaviour the wildcard was there for, over an InMemoryEventBus."""
    cache = InMemoryCache()
    await cache.start()
    adapter = QueryCacheAdapter(cache=cache)
    await adapter.put("order:42", {"id": "42"}, ttl=300)

    bus = InMemoryEventBus()
    await bus.start()
    b = EdaCacheInvalidationBridge(adapter)
    b.register("order.updated", "order:{order_id}")
    b.subscribe(bus)

    await bus.publish("pyfly.events", "order.updated", {"order_id": "42"})
    assert await adapter.get("order:42") is None


@pytest.mark.asyncio
async def test_an_unregistered_event_type_never_reaches_the_bridge() -> None:
    """Previously it arrived and was discarded; now it is not delivered at all."""
    seen: list[EventEnvelope] = []
    b = bridge()

    async def spy(envelope: EventEnvelope) -> None:
        seen.append(envelope)

    b.on_envelope = spy  # type: ignore[method-assign]
    bus = InMemoryEventBus()
    await bus.start()
    b.register("order.updated", "order:{order_id}")
    b.subscribe(bus)

    await bus.publish("pyfly.events", "ingest.source.requested", {"source_id": "1"})
    assert seen == []


class TestOffSwitch:
    """``pyfly.cqrs.cache.invalidation.enabled`` — refusing the bridge without disabling CQRS."""

    @staticmethod
    def _included(enabled: str | None) -> bool:
        raw: dict[str, object] = (
            {} if enabled is None else {"pyfly": {"cqrs": {"cache": {"invalidation": {"enabled": enabled}}}}}
        )
        evaluator = ConditionEvaluator(Config(raw), Container())
        return evaluator.should_include_method(CqrsAutoConfiguration.eda_cache_invalidation_bridge)

    def test_the_bean_is_wired_by_default(self) -> None:
        assert self._included(None) is True

    def test_the_property_refuses_the_bean(self) -> None:
        assert self._included("false") is False

    def test_the_property_set_to_true_keeps_the_bean(self) -> None:
        assert self._included("true") is True

    def test_the_property_defaults_to_on(self) -> None:
        assert CqrsProperties().cache.invalidation.enabled is True
