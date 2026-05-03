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
"""Tests for ApplicationContext decorator wiring (Phase 3 audit fixes)."""

import pytest

from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import (
    ApplicationReadyEvent,
    ContextRefreshedEvent,
    app_event_listener,
)
from pyfly.core.config import Config

# --- Test: BeanPostProcessor auto-discovery ---


class TestBeanPostProcessorDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_post_processor_beans(self):
        """BeanPostProcessors registered as beans are auto-discovered."""
        call_log: list[str] = []

        @service
        class LoggingPostProcessor:
            def before_init(self, bean, bean_name):
                call_log.append(f"before:{bean_name}")
                return bean

            def after_init(self, bean, bean_name):
                call_log.append(f"after:{bean_name}")
                return bean

        @service
        class SomeService:
            pass

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(LoggingPostProcessor)
        ctx.register_bean(SomeService)
        await ctx.start()

        assert any("before:" in entry for entry in call_log)
        assert any("after:" in entry for entry in call_log)
        assert ctx.wiring_counts.get("post_processors", 0) >= 1


# --- Test: @app_event_listener wiring ---


class TestAppEventListenerWiring:
    @pytest.mark.asyncio
    async def test_wires_event_listeners_to_event_bus(self):
        """Methods decorated with @app_event_listener are subscribed to the event bus."""
        events_received: list = []

        @service
        class MyListener:
            @app_event_listener
            async def on_ready(self, event: ApplicationReadyEvent):
                events_received.append(event)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyListener)
        await ctx.start()

        # ApplicationReadyEvent is published during start()
        assert any(isinstance(e, ApplicationReadyEvent) for e in events_received)

    @pytest.mark.asyncio
    async def test_event_listener_count_tracked(self):
        @service
        class ListenerA:
            @app_event_listener
            async def on_refreshed(self, event: ContextRefreshedEvent):
                pass

        @service
        class ListenerB:
            @app_event_listener
            async def on_ready(self, event: ApplicationReadyEvent):
                pass

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ListenerA)
        ctx.register_bean(ListenerB)
        await ctx.start()

        assert ctx.wiring_counts["event_listeners"] == 2


# --- Test: @message_listener wiring ---


class TestMessageListenerWiring:
    @pytest.mark.asyncio
    async def test_skips_when_no_broker(self):
        """If no MessageBrokerPort is registered, @message_listener wiring is skipped gracefully."""
        from pyfly.messaging.decorators import message_listener

        @service
        class MyConsumer:
            @message_listener(topic="orders", group="order-group")
            async def handle_order(self, msg):
                pass

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyConsumer)
        await ctx.start()  # Should not raise
        assert ctx.wiring_counts.get("message_listeners", 0) == 0


# --- Test: @command_handler / @query_handler wiring ---


class TestCQRSHandlerWiring:
    @pytest.mark.asyncio
    async def test_skips_when_no_registry(self):
        """If no HandlerRegistry is registered, CQRS handler wiring is skipped gracefully."""
        from dataclasses import dataclass

        from pyfly.cqrs.command.handler import CommandHandler
        from pyfly.cqrs.decorators import command_handler
        from pyfly.cqrs.types import Command

        @dataclass
        class CreateOrder(Command[str]):
            name: str = ""

        @command_handler
        @service
        class CreateOrderHandler(CommandHandler[CreateOrder, str]):
            async def do_handle(self, command: CreateOrder) -> str:
                return "created"

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(CreateOrderHandler)
        await ctx.start()  # Should not raise
        assert ctx.wiring_counts.get("cqrs_handlers", 0) == 0


# --- Test: @scheduled wiring ---


class TestScheduledWiring:
    @pytest.mark.asyncio
    async def test_discovers_scheduled_methods(self):
        """@scheduled methods are discovered and count is tracked."""
        from datetime import timedelta

        from pyfly.scheduling.decorators import scheduled

        @service
        class MyScheduledTask:
            @scheduled(fixed_rate=timedelta(seconds=60))
            async def run_periodically(self):
                pass

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyScheduledTask)
        await ctx.start()

        assert ctx.wiring_counts["scheduled"] == 1

        # Clean up scheduler
        await ctx.stop()


# --- Test: @async_method wiring ---


class TestAsyncMethodWiring:
    @pytest.mark.asyncio
    async def test_wraps_async_methods(self):
        """@async_method decorated methods are wrapped for async execution."""
        from pyfly.scheduling.decorators import async_method

        @service
        class MyService:
            @async_method
            def heavy_computation(self):
                return 42

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(MyService)
        await ctx.start()

        assert ctx.wiring_counts["async_methods"] == 1

        await ctx.stop()


# --- Test: Registry stats ---


class TestRegistryStats:
    @pytest.mark.asyncio
    async def test_stereotype_counts(self):
        from pyfly.container.stereotypes import repository

        @service
        class Svc:
            pass

        @repository
        class Repo:
            pass

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(Svc)
        ctx.register_bean(Repo)
        await ctx.start()

        counts = ctx.get_bean_counts_by_stereotype()
        assert counts.get("service", 0) >= 1
        assert counts.get("repository", 0) >= 1
