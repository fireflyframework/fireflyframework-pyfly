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
"""The command bus publishes a command's events after the commit, and invalidates when the outcome is unknown.

- C144 (CQRS): ``bus.send()`` inside a wider unit of work published the command's events to the broker at
  once; when that unit then rolled back, the events were out and the change was not. Events now go through
  an ``after_commit`` synchronization (at once outside a unit), and an outbox bus writes them in the unit.
- REJECTED #11: a ``DomainEvent`` (its ``occurred_at`` is a ``datetime``) could not be serialized on any bus,
  and the publisher logged the ``TypeError`` away. The CQRS publisher sends the event's JSON payload.
- WP14 hand-off: a native ``task.cancel()`` landing in a handler's own ``@transactional`` commit (which
  completes, shielded) skipped the query-cache invalidation. The bus now invalidates when the handler ends
  with a cancellation or an unknown commit outcome.

A real ``ApplicationContext`` on every relational lane (SQLite file with foreign keys on, PostgreSQL, MySQL,
MariaDB).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.bus import DefaultCommandBus, EventFailureStrategy
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.event.publisher import EdaCommandEventPublisher
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import transactional
from pyfly.domain import DomainEvent
from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.types import EventEnvelope
from tests.support.backend_matrix import RelationalBackend


class CommandedItem(Base):
    __tablename__ = "wp09_commanded_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    label: Mapped[str] = mapped_column(String(64))


@repository
class CommandedItemRepository(Repository[CommandedItem, int]):
    pass


@dataclass(frozen=True)
class ItemRenamed(DomainEvent):
    item_id: int = 0
    label: str = ""


@dataclass
class RenameResult:
    domain_events: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class RenameItem(Command[RenameResult]):
    item_id: int = 0
    label: str = ""

    def get_cache_key(self) -> str | None:
        return GetItem(item_id=self.item_id).get_cache_key()


@dataclass(frozen=True)
class GetItem(Query[str]):
    item_id: int = 0


@command_handler
class RenameItemHandler(CommandHandler[RenameItem, RenameResult]):
    def __init__(self, items: CommandedItemRepository) -> None:
        super().__init__()
        self.items = items
        self.on_return: Callable[[], None] = lambda: None

    @transactional
    async def do_handle(self, command: RenameItem) -> RenameResult:
        await self.items._session.execute(
            update(CommandedItem).where(CommandedItem.id == command.item_id).values(label=command.label)
        )
        self.on_return()
        return RenameResult(domain_events=[ItemRenamed(item_id=command.item_id, label=command.label)])


@query_handler(cacheable=True)
class GetItemHandler(QueryHandler[GetItem, str]):
    def __init__(self, items: CommandedItemRepository) -> None:
        super().__init__()
        self.items = items
        self.calls = 0

    async def do_handle(self, query: GetItem) -> str:
        self.calls += 1
        item = await self.items.find_by_id(query.item_id)
        assert item is not None
        return item.label


class RecordingBroker:
    """A broker publisher that does not join transactions (Kafka, RabbitMQ): what it got, and when."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def subscribe(self, pattern: str, handler: Any) -> None:
        del pattern, handler

    async def publish(
        self, destination: str, event_type: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        del destination, headers
        import json

        json.dumps(payload)  # what every broker serializer does
        self.published.append((event_type, payload))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@service
class Catalog:
    """Sends the command inside a wider unit of work, which may fail afterwards."""

    def __init__(self) -> None:
        self.commands: DefaultCommandBus | None = None

    @transactional
    async def rename_and_then(self, item_id: int, label: str, *, fail: bool) -> None:
        assert self.commands is not None
        await self.commands.send(RenameItem(item_id=item_id, label=label))
        if fail:
            raise RuntimeError("pricing refused the new label")


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    await backend.create_tables(CommandedItem)
    ctx = ApplicationContext(backend.config())
    for bean in (RelationalAutoConfiguration, CommandedItemRepository, Catalog):
        ctx.register_bean(bean)
    await ctx.start()
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO wp09_commanded_item (label) VALUES ('old')"))
    finally:
        await engine.dispose()
    return ctx


def _buses(ctx: ApplicationContext, publisher: Any, cache: QueryCacheAdapter | None = None) -> Any:
    items = ctx.get_bean(CommandedItemRepository)
    registry = HandlerRegistry()
    renames, gets = RenameItemHandler(items), GetItemHandler(items)
    registry.register_command_handler(renames)
    registry.register_query_handler(gets)
    commands = DefaultCommandBus(
        registry=registry,
        event_publisher=publisher,
        event_failure_strategy=EventFailureStrategy.RAISE,
        query_cache=cache,
    )
    return commands, DefaultQueryBus(registry=registry, cache_adapter=cache), renames, gets


async def test_events_of_a_command_in_a_unit_that_rolls_back_are_not_published(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        broker = RecordingBroker()
        commands, *_ = _buses(ctx, EdaCommandEventPublisher(broker))
        catalog = ctx.get_bean(Catalog)
        catalog.commands = commands

        with pytest.raises(RuntimeError, match="pricing refused"):
            await catalog.rename_and_then(1, "new", fail=True)
        assert broker.published == []

        await catalog.rename_and_then(1, "newer", fail=False)
        assert [event_type for event_type, _payload in broker.published] == ["ItemRenamed"]
        payload = broker.published[0][1]
        assert payload["label"] == "newer"
        assert isinstance(payload["occurred_at"], str)  # the DomainEvent's datetime, as ISO-8601
    finally:
        await ctx.stop()


async def test_an_outbox_bus_takes_the_command_events_in_the_unit(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    bus = DatabaseEventBus(group="catalog", notify=False, poll_interval=0.05)
    received: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)

    bus.subscribe("ItemRenamed", handler)
    await bus.start()
    try:
        commands, *_ = _buses(ctx, EdaCommandEventPublisher(bus, default_destination="catalog"))
        catalog = ctx.get_bean(Catalog)
        catalog.commands = commands

        with pytest.raises(RuntimeError, match="pricing refused"):
            await catalog.rename_and_then(1, "new", fail=True)
        await catalog.rename_and_then(1, "newer", fail=False)
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        assert [envelope.payload["label"] for envelope in received] == ["newer"]
    finally:
        await bus.stop()
        await ctx.stop()


async def test_a_command_cancelled_as_its_own_unit_commits_still_invalidates(
    relational_backend: RelationalBackend,
) -> None:
    """WP14 hand-off: asyncio's own cancellation, delivered once, at the handler's commit."""
    ctx = await _boot(relational_backend)
    try:
        cache = QueryCacheAdapter(InMemoryCache())
        commands, queries, renames, gets = _buses(ctx, None, cache)
        caller = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
        assert await queries.query_with_context(GetItem(item_id=1), caller) == "old"

        def cancel_this_task() -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

        renames.on_return = cancel_this_task
        task = asyncio.create_task(commands.send(RenameItem(item_id=1, label="renamed")))
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await queries.query_with_context(GetItem(item_id=1), caller) == "renamed"
        assert gets.calls == 2
    finally:
        await ctx.stop()
