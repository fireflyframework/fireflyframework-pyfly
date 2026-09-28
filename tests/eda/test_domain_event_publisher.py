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
"""``DomainEventPublisher`` on a SQLite file database's units of work (C144).

The end-to-end matrix (ORM aggregates, the outbox bus, SQLite and PostgreSQL) is
``tests/integration/test_domain_events_publication.py``; this pins the publisher's own rules: what it
collects, what a listener raising more events does, the publishers of two contexts, and what it leaves alone.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    TransactionPhase,
    TransactionSynchronizationAdapter,
    TransactionTemplate,
    current_unit_of_work,
)
from pyfly.domain import AggregateRoot, DomainEvent
from pyfly.eda.domain_events import MAX_PUBLICATION_ROUNDS, DomainEventPublisher, active_domain_event_publisher
from tests.support.backend_matrix import enable_sqlite_foreign_keys


@dataclass(frozen=True)
class Opened(DomainEvent):
    account: str = ""


@dataclass(frozen=True)
class Welcomed(DomainEvent):
    account: str = ""


class Account(AggregateRoot[str]):
    def open(self) -> None:
        assert self.id is not None
        self.raise_event(Opened(account=self.id))


@pytest.fixture
async def template(tmp_path: Path) -> AsyncIterator[TransactionTemplate]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    enable_sqlite_foreign_keys(engine)
    try:
        yield TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    finally:
        await engine.dispose()


@pytest.fixture
async def bus() -> AsyncIterator[ApplicationEventBus]:
    bus = ApplicationEventBus()
    publisher = DomainEventPublisher(ApplicationEventPublisher(bus))
    await publisher.start()
    try:
        yield bus
    finally:
        await publisher.stop()


async def test_events_raised_in_a_unit_reach_the_listeners_as_it_commits(
    template: TransactionTemplate, bus: ApplicationEventBus
) -> None:
    seen: list[tuple[str, str]] = []

    async def inline(event: Opened) -> None:
        seen.append(("inline", event.account))

    async def after_commit(event: Opened) -> None:
        seen.append(("after_commit", event.account))

    bus.subscribe(Opened, inline)
    bus.subscribe(Opened, after_commit, phase=TransactionPhase.AFTER_COMMIT)
    account = Account("a-1")

    async with template.transaction():
        account.open()
        assert seen == []  # nothing before the commit
    assert seen == [("inline", "a-1"), ("after_commit", "a-1")]
    assert account.pending_events() == []


async def test_a_unit_that_rolls_back_publishes_nothing_and_leaves_the_events_pending(
    template: TransactionTemplate, bus: ApplicationEventBus
) -> None:
    seen: list[str] = []

    async def listener(event: Opened) -> None:
        seen.append(event.account)

    bus.subscribe(Opened, listener)
    account = Account("a-2")
    with pytest.raises(RuntimeError):
        async with template.transaction():
            account.open()
            raise RuntimeError("the account could not be opened")
    assert seen == []
    assert [type(event) for event in account.pending_events()] == [Opened]

    async with template.transaction():  # saved again: the events go out with that unit
        account.raise_event(Welcomed(account="a-2"))
    assert seen == ["a-2"]
    assert account.pending_events() == []


async def test_events_raised_outside_a_unit_stay_pending(
    bus: ApplicationEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """They are published when a unit saves this instance; a debug record says so (a detached copy merged into
    an instance the unit holds would keep them pending)."""
    with caplog.at_level(logging.DEBUG, logger="pyfly.eda.domain_events"):
        account = Account("a-3")
        account.open()
    assert [type(event) for event in account.pending_events()] == [Opened]
    assert [record.message for record in caplog.records] == ["domain_event_pending_outside_unit"]
    assert caplog.records[0].aggregate_type == "Account"


async def test_a_listener_that_raises_more_events_gets_them_published_in_the_same_unit(
    template: TransactionTemplate, bus: ApplicationEventBus
) -> None:
    seen: list[str] = []
    welcomes = Account("mailer")

    async def on_opened(event: Opened) -> None:
        welcomes.raise_event(Welcomed(account=event.account))

    async def before_commit(event: Welcomed) -> None:
        seen.append(f"welcome {event.account}")

    bus.subscribe(Opened, on_opened)
    bus.subscribe(Welcomed, before_commit, phase=TransactionPhase.BEFORE_COMMIT)
    async with template.transaction():
        Account("a-4").open()
    assert seen == ["welcome a-4"]


async def test_a_synchronization_a_before_commit_listener_puts_first_runs_once_and_shifts_nothing(
    template: TransactionTemplate, bus: ApplicationEventBus
) -> None:
    """The synchronizations registered while a unit's events are published run their before-commit part once each,
    whatever position one of them takes in the unit's list (the document backend puts one first)."""
    runs: list[str] = []

    class Recorded(TransactionSynchronizationAdapter):
        async def before_commit(self, read_only: bool) -> None:
            runs.append("put first")

    async def before_commit(event: Opened) -> None:
        runs.append(f"listener {event.account}")
        unit = current_unit_of_work()
        assert unit is not None
        if "put first" not in runs and not any(isinstance(item, Recorded) for item in unit.synchronizations):
            unit.synchronizations.insert(0, Recorded())

    bus.subscribe(Opened, before_commit, phase=TransactionPhase.BEFORE_COMMIT)
    async with template.transaction():
        Account("a-5").open()
    assert runs == ["listener a-5", "put first"]


async def test_listeners_that_keep_raising_events_roll_the_unit_back(
    template: TransactionTemplate, bus: ApplicationEventBus
) -> None:
    loop = Account("loop")

    async def again(event: Opened) -> None:
        loop.raise_event(Opened(account=event.account))

    bus.subscribe(Opened, again)
    with pytest.raises(RuntimeError, match=f"{MAX_PUBLICATION_ROUNDS} rounds"):
        async with template.transaction():
            loop.open()


async def test_the_last_started_publisher_collects_and_stopping_hands_back() -> None:
    first = DomainEventPublisher()
    second = DomainEventPublisher()
    await first.start()
    await second.start()
    try:
        assert active_domain_event_publisher() is second
        await second.stop()
        assert active_domain_event_publisher() is first
        await second.stop()  # idempotent
    finally:
        await first.stop()
    assert active_domain_event_publisher() is None
