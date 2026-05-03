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
"""Tests for PyFly testing utilities."""

import pytest

from pyfly.container import Container
from pyfly.context.application_context import ApplicationContext
from pyfly.eda import EventEnvelope
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.testing.assertions import assert_event_published, assert_no_events_published
from pyfly.testing.containers import create_test_container
from pyfly.testing.fixtures import PyFlyTestCase


class TestPyFlyTestCase:
    @pytest.mark.asyncio
    async def test_has_context(self):
        tc = PyFlyTestCase()
        await tc.setup()
        assert isinstance(tc.context, ApplicationContext)
        await tc.teardown()

    @pytest.mark.asyncio
    async def test_has_event_bus(self):
        tc = PyFlyTestCase()
        await tc.setup()
        assert isinstance(tc.event_bus, InMemoryEventBus)
        await tc.teardown()

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        tc = PyFlyTestCase()
        await tc.setup()
        # Container should be functional via context
        tc.context.container.register(list)
        instance = tc.context.container.resolve(list)
        assert isinstance(instance, list)
        await tc.teardown()


class TestEventAssertions:
    @pytest.mark.asyncio
    async def test_assert_event_published(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("*", capture)
        await bus.publish("topic", "user.created", {"id": "1"})

        assert_event_published(published, "user.created")

    @pytest.mark.asyncio
    async def test_assert_event_not_published(self):
        published: list[EventEnvelope] = []

        with pytest.raises(AssertionError, match="Expected event"):
            assert_event_published(published, "user.created")

    @pytest.mark.asyncio
    async def test_assert_no_events(self):
        published: list[EventEnvelope] = []
        assert_no_events_published(published)

    @pytest.mark.asyncio
    async def test_assert_no_events_fails_when_events_exist(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("*", capture)
        await bus.publish("topic", "order.created", {"id": "1"})

        with pytest.raises(AssertionError, match="Expected no events"):
            assert_no_events_published(published)

    @pytest.mark.asyncio
    async def test_assert_event_with_payload_check(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("*", capture)
        await bus.publish("topic", "user.created", {"id": "1", "name": "Alice"})

        assert_event_published(published, "user.created", payload_contains={"name": "Alice"})


class TestCreateTestContainer:
    def test_creates_container_with_defaults(self):
        container = create_test_container()
        assert isinstance(container, Container)

    def test_registers_overrides(self):
        class FakeDB:
            pass

        container = create_test_container(overrides={object: FakeDB})
        instance = container.resolve(object)
        assert isinstance(instance, FakeDB)
