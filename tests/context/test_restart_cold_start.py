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
"""A stopped context that is started again behaves like a cold start (C100).

``stop()`` released the singletons but kept four pieces of the previous run: the list of started
adapters (so ``start()`` restarted the stopped ones beside the new ones), the post-processors
discovered from beans (so the new beans were post-processed by the previous run's instances), the
event-bus subscriptions (so every event reached the previous run's listeners too) and the post-create
hook (so ``@post_construct`` and advice ran twice or more). Each run must now see one instance of
everything, as a fresh context does.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from pyfly.aop.decorators import around, aspect  # noqa: E402
from pyfly.container import bean, component, configuration, service  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.context.events import ApplicationEventPublisher, app_event_listener  # noqa: E402
from pyfly.context.lifecycle import post_construct  # noqa: E402
from pyfly.core.config import Config  # noqa: E402

CALLS: list[tuple[str, int]] = []


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


class OrderPlaced:
    pass


@service
class _Orders:
    def __init__(self, events: ApplicationEventPublisher) -> None:
        self.events = events

    @post_construct
    def ready(self) -> None:
        CALLS.append(("post_construct", id(self)))

    @app_event_listener
    async def on_order(self, event: OrderPlaced) -> None:
        CALLS.append(("listener", id(self)))

    async def ping(self) -> str:
        return "pong"


@aspect
class _Tracing:
    @around("service._Orders.ping")
    async def trace(self, join_point: Any) -> Any:
        CALLS.append(("advice", id(self)))
        return await join_point.proceed()


class _Relay:
    """A lifecycle bean that writes to the database when it starts."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def start(self) -> None:
        async with self._factory() as session:
            await session.execute(text("SELECT 1"))
        CALLS.append(("relay.start", id(self)))

    async def stop(self) -> None:
        CALLS.append(("relay.stop", id(self)))


@configuration
class _RelayConfiguration:
    @bean
    def relay(self, factory: async_sessionmaker[AsyncSession]) -> _Relay:
        return _Relay(factory)


@component
class _Marker:
    """A BeanPostProcessor discovered from the beans: it must be the new run's instance."""

    def before_init(self, bean: Any, bean_name: str) -> Any:
        if isinstance(bean, _Orders):
            CALLS.append(("post_processor", id(self)))
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        return bean


def _context(tmp_path: Path) -> ApplicationContext:
    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": "true",
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                        "ddl-auto": "none",
                    }
                }
            }
        }
    )
    context = ApplicationContext(config)
    for bean_class in (_Orders, _Tracing, _RelayConfiguration, _Marker):
        context.register_bean(bean_class)
    return context


async def _run(context: ApplicationContext) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Start, place one order, ping once, stop; the calls of this run and the ids of its beans."""
    first = len(CALLS)
    await context.start()
    ids = {
        "orders": id(context.get_bean(_Orders)),
        "tracing": id(context.get_bean(_Tracing)),
        "relay": id(context.get_bean(_Relay)),
        "marker": id(context.get_bean(_Marker)),
        "engine": id(context.get_bean(AsyncEngine)),
    }
    await context.get_bean(ApplicationEventPublisher).publish(OrderPlaced())
    assert await context.get_bean(_Orders).ping() == "pong"
    await context.stop()
    return CALLS[first:], ids


def _expected(ids: dict[str, int]) -> list[tuple[str, int]]:
    return [
        ("relay.start", ids["relay"]),
        ("post_processor", ids["marker"]),
        ("post_construct", ids["orders"]),
        ("listener", ids["orders"]),
        ("advice", ids["tracing"]),
        ("relay.stop", ids["relay"]),
    ]


async def test_a_restart_reproduces_a_cold_start(tmp_path: Path) -> None:
    context = _context(tmp_path)

    first_calls, first_ids = await _run(context)
    assert first_calls == _expected(first_ids)

    second_calls, second_ids = await _run(context)
    assert second_calls == _expected(second_ids)
    assert all(second_ids[name] != first_ids[name] for name in ("orders", "relay", "marker"))


async def test_stop_leaves_nothing_of_the_run_behind(tmp_path: Path) -> None:
    context = _context(tmp_path)
    await context.start()
    await context.stop()

    assert context._lifecycle_beans == []
    assert context._container._post_create_hook is None
    assert context.event_bus.listener_count(OrderPlaced) == 0
    assert [type(pp) for pp in context._post_processors] == []
