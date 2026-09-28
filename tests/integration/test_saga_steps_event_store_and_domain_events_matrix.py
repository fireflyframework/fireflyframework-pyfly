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
"""Saga steps with the event store and domain events, on sqlite-file and PostgreSQL.

The integration of the orchestration engines (WP13) with the event store (WP08) and domain events (WP09):

- a saga step runs in a detached task, so its work is its own unit of work, never its caller's; the event
  store joins the unit it finds, so a step's append commits with the step and outlives a caller that rolls
  back;
- the unit the event store opens on its own counts as the step's commit (``track_commits``): a step that
  appended and then failed is compensated, and never retried;
- the domain events an aggregate raises in a step are published as the step's unit commits, while the saga
  still runs, not when the caller's unit ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import app_event_listener
from pyfly.data.relational.framework_schema import event_store, event_store_head, snapshots
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import TransactionPhase, transactional
from pyfly.domain import AggregateRoot, DomainEvent
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.store import EventStore
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

ACCOUNT = "wp13-account-1"


@dataclass(frozen=True)
class AccountRegistered(DomainEvent):
    number: str = ""


class RegisteredAccount(Base, AggregateRoot[int]):
    __tablename__ = "wp13_registered_account"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    number: Mapped[str] = mapped_column(String(64))

    @classmethod
    def register(cls, number: str) -> RegisteredAccount:
        account = cls(number=number)
        account.raise_event(AccountRegistered(number=number))
        return account


class CallerRow(Base):
    __tablename__ = "wp13_caller_row"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    note: Mapped[str] = mapped_column(String(64))


@repository
class RegisteredAccounts(Repository[RegisteredAccount, int]):
    pass


@repository
class CallerRows(Repository[CallerRow, int]):
    pass


class OpenFailedError(Exception):
    """The ``open`` step fails after its append committed."""


class NotifyFailedError(Exception):
    """The ``notify`` step fails: the saga compensates ``open``."""


class Script:
    def __init__(self) -> None:
        self.fail_open = False
        self.fail_notify = False
        self.open_attempts = 0
        self.registered = asyncio.Event()
        self.published_while_running: list[bool] = []


SCRIPT = Script()


@service
class Notifications:
    """Hears the domain event once the unit that saved the aggregate committed."""

    def __init__(self) -> None:
        self.after_commit: list[str] = []

    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def on_registered(self, event: AccountRegistered) -> None:
        self.after_commit.append(event.number)
        SCRIPT.registered.set()


@saga(name="wp13-open-account")
class OpenAccount:
    """``open`` appends to the event store and saves an aggregate that raises a domain event; ``notify``, in the
    next layer, waits for that domain event, which only its publication at ``open``'s commit delivers."""

    def __init__(self, store: EventStore, accounts: RegisteredAccounts) -> None:
        self.store = store
        self.accounts = accounts

    @saga_step(id="open", compensate="close", retry=2)
    async def open(self) -> None:
        SCRIPT.open_attempts += 1
        await self.store.append(ACCOUNT, "Account", [StoredEventEnvelope(event_type="Opened")], expected_version=0)
        if SCRIPT.fail_open:
            raise OpenFailedError("the account could not be opened")
        await self.accounts.save(RegisteredAccount.register(ACCOUNT))

    async def close(self) -> None:
        version = await self.store.latest_version(ACCOUNT)
        await self.store.append(
            ACCOUNT, "Account", [StoredEventEnvelope(event_type="Closed")], expected_version=version
        )

    @saga_step(id="notify", depends_on=["open"])
    async def notify(self) -> None:
        await asyncio.wait_for(SCRIPT.registered.wait(), timeout=10)
        if SCRIPT.fail_notify:
            raise NotifyFailedError("the welcome mail bounced")


@service
class Caller:
    """A caller whose own unit writes, runs the saga, and then fails."""

    def __init__(self, rows: CallerRows, sagas: SagaEngine) -> None:
        self.rows = rows
        self.sagas = sagas

    @transactional
    async def open_then_fail(self) -> Any:
        await self.rows.save(CallerRow(note="caller"))
        result = await self.sagas.execute("wp13-open-account")
        raise RuntimeError(f"the caller fails after the saga ({result.success})")


@pytest.fixture
async def context(relational_backend: RelationalBackend) -> AsyncIterator[ApplicationContext]:
    global SCRIPT
    SCRIPT = Script()
    await relational_backend.create_tables(RegisteredAccount, CallerRow)
    await relational_backend.create_tables(event_store, event_store_head, snapshots)  # ddl-auto none: as migrations
    ctx = ApplicationContext(
        relational_backend.config(
            {
                "pyfly.transactional.enabled": "true",
                "pyfly.eventsourcing.enabled": "true",
                "pyfly.eventsourcing.store.provider": "sqlalchemy",
                "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
                "pyfly.eda.provider": "memory",
            }
        )
    )
    for bean in (RegisteredAccounts, CallerRows, Notifications, OpenAccount, Caller):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield ctx
    finally:
        await ctx.stop()


async def _committed(backend: RelationalBackend, sql: str) -> list[Any]:
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return [row[0] for row in (await connection.execute(text(sql))).all()]
    finally:
        await engine.dispose()


async def _event_types(backend: RelationalBackend) -> list[str]:
    return await _committed(
        backend, f"SELECT event_type FROM pyfly_event_store WHERE aggregate_id = '{ACCOUNT}' ORDER BY sequence"
    )


async def test_a_steps_append_and_domain_events_commit_with_the_step(
    context: ApplicationContext, relational_backend: RelationalBackend
) -> None:
    SCRIPT.fail_notify = True
    result = await context.get_bean(SagaEngine).execute("wp13-open-account")

    assert result.success is False
    assert isinstance(result.error, NotifyFailedError)  # notify heard the domain event: published at open's commit
    assert context.get_bean(Notifications).after_commit == [ACCOUNT]
    assert result.steps["open"].compensated is True
    assert await _event_types(relational_backend) == ["Opened", "Closed"]


async def test_a_step_that_appended_and_then_failed_is_compensated_and_never_retried(
    context: ApplicationContext, relational_backend: RelationalBackend
) -> None:
    SCRIPT.fail_open = True
    result = await context.get_bean(SagaEngine).execute("wp13-open-account")

    assert result.success is False
    assert isinstance(result.error, OpenFailedError)
    assert SCRIPT.open_attempts == 1  # its append committed: a retry would append twice
    assert await _event_types(relational_backend) == ["Opened", "Closed"]
    assert context.get_bean(Notifications).after_commit == []


@pytest.mark.backends(PG)
async def test_what_the_steps_committed_outlives_a_caller_that_rolls_back(
    context: ApplicationContext, relational_backend: RelationalBackend
) -> None:
    with pytest.raises(RuntimeError, match=r"the caller fails after the saga \(True\)"):
        await context.get_bean(Caller).open_then_fail()

    assert await _committed(relational_backend, "SELECT note FROM wp13_caller_row") == []  # the caller's unit
    assert await _event_types(relational_backend) == ["Opened"]  # the step's own units
    assert await _committed(relational_backend, "SELECT number FROM wp13_registered_account") == [ACCOUNT]
    assert context.get_bean(Notifications).after_commit == [ACCOUNT]
