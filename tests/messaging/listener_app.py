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
"""A real application for the listener container tests: an ``ApplicationContext`` on a SQLite file
database (foreign keys on), a repository, and listeners whose behavior a :class:`Behavior` scripts."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container import bean, configuration
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transactional import transactional
from pyfly.eda.decorators import event_listener
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.messaging.decorators import message_listener
from pyfly.messaging.ports.outbound import MessageBrokerPort
from pyfly.messaging.types import Message
from tests.support.backend_matrix import RelationalBackend


class Delivered(Base):
    __tablename__ = "wp12_delivered"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    body: Mapped[str] = mapped_column(String(64))


@repository
class DeliveredRepository(Repository[Delivered, int]):
    pass


@dataclass
class Behavior:
    """What a listener does with each body, and what it saw."""

    fail_first: set[str] = field(default_factory=set)
    always_fail: set[str] = field(default_factory=set)
    errors: dict[str, Callable[[], Exception]] = field(default_factory=dict)
    """Bodies that raise this error (built fresh each time) on every attempt."""
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    """Bodies whose handler waits for this event inside its transaction, after writing."""
    work: float = 0.0
    attempts: Counter[str] = field(default_factory=Counter)
    seen: list[str] = field(default_factory=list)
    units: list[int | None] = field(default_factory=list)
    started: dict[str, asyncio.Event] = field(default_factory=dict)
    finished: list[str] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    running: int = 0
    peak: int = 0

    def started_event(self, body: str) -> asyncio.Event:
        return self.started.setdefault(body, asyncio.Event())

    async def handle(self, repo: DeliveredRepository, body: str, delivered: Any = None) -> None:
        self.attempts[body] += 1
        self.seen.append(body)
        self.messages.append(delivered)
        unit = current_unit_of_work()
        self.units.append(unit.id if unit is not None else None)
        self.started_event(body).set()
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await repo.save(Delivered(body=body))
            if self.work:
                await asyncio.sleep(self.work)
            gate = self.gates.get(body)
            if gate is not None:
                await gate.wait()
            if body in self.errors:
                raise self.errors[body]()
            if body in self.always_fail or (body in self.fail_first and self.attempts[body] == 1):
                raise RuntimeError(f"listener fails on {body} (attempt {self.attempts[body]})")
            self.finished.append(body)
        finally:
            self.running -= 1


def message_listener_bean(topic: str, group: str | None, behavior: Behavior, **options: Any) -> type:
    """A ``@service`` with one ``@message_listener`` + ``@transactional`` method that saves a row."""

    @service
    class OrderMessages:
        def __init__(self, repo: DeliveredRepository) -> None:
            self.repo = repo

        @message_listener(topic, group=group, **options)
        @transactional
        async def on_message(self, message: Message) -> None:
            await behavior.handle(self.repo, message.value.decode(), message)

    return OrderMessages


def event_listener_bean(behavior: Behavior, pattern: str = "order.*") -> type:
    """A ``@service`` with one ``@event_listener`` + ``@transactional`` method that saves a row."""

    @service
    class OrderEvents:
        def __init__(self, repo: DeliveredRepository) -> None:
            self.repo = repo

        @event_listener([pattern])
        @transactional
        async def on_event(self, envelope: EventEnvelope) -> None:
            await behavior.handle(self.repo, str(envelope.payload["body"]), envelope)

    return OrderEvents


def broker_bean(broker: Any) -> type:
    """A ``@configuration`` whose ``@bean`` is *broker* (a ``MessageBrokerPort``)."""

    @configuration
    class BrokerConfiguration:
        @bean
        def message_broker(self) -> MessageBrokerPort:
            return broker  # type: ignore[no-any-return]

    return BrokerConfiguration


def event_bus_bean(bus: Any) -> type:
    """A ``@configuration`` whose ``@bean`` is *bus* (an ``EventPublisher``)."""

    @configuration
    class BusConfiguration:
        @bean
        def event_publisher(self) -> EventPublisher:
            return bus  # type: ignore[no-any-return]

    return BusConfiguration


async def boot(backend: RelationalBackend, *beans: type, overrides: dict[str, Any] | None = None) -> ApplicationContext:
    """Create the table and start a context with the relational auto-configuration and *beans*."""
    await backend.create_tables(Delivered)
    ctx = ApplicationContext(backend.config({"pyfly.messaging.provider": "memory", **(overrides or {})}))
    for registered in (RelationalAutoConfiguration, DeliveredRepository, *beans):
        ctx.register_bean(registered)
    await ctx.start()
    return ctx


async def committed_bodies(backend: RelationalBackend) -> list[str]:
    """The committed rows, as another connection sees them."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return sorted(row[0] for row in (await conn.execute(text("SELECT body FROM wp12_delivered"))).all())
    finally:
        await engine.dispose()


async def eventually(check: Callable[[], Awaitable[bool] | bool], *, timeout: float = 10.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if isinstance(outcome, Awaitable):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what or check}")
        await asyncio.sleep(0.02)


FAST_RETRY: dict[str, Any] = {
    "retry.initial-delay": "0.01",
    "retry.multiplier": "1.0",
    "retry.max-attempts": "3",
}


def listener_config(prefix: str, **overrides: Any) -> dict[str, Any]:
    """``<prefix>.listener.*`` keys: the fast retry policy plus *overrides* (``shutdown_timeout=...``)."""
    values = {**FAST_RETRY, **{key.replace("_", "-"): value for key, value in overrides.items()}}
    return {f"{prefix}.listener.{key}": value for key, value in values.items()}
