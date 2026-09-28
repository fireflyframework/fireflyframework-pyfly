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
"""``@app_event_listener(phase=...)``: application event listeners bound to the transaction (C144).

An ``@app_event_listener`` ran inline in ``publish()``, inside the caller's transaction: a listener that sent an
e-mail or called a remote API did so even when the transaction then rolled back. A listener now declares the
:class:`~pyfly.data.transaction.TransactionPhase` it runs at (Spring's ``@TransactionalEventListener``):

- ``BEFORE_COMMIT`` inside the unit, right before it commits;
- ``AFTER_COMMIT`` once it committed (not at all when it rolls back);
- ``AFTER_ROLLBACK`` only when it rolled back;
- ``AFTER_COMPLETION`` either way;

and outside a transaction every phase but ``AFTER_ROLLBACK`` runs at once. A listener with no phase still runs
inline. A real ``ApplicationContext`` on every relational lane (SQLite file with foreign keys on, PostgreSQL,
MySQL, MariaDB).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import ApplicationEventPublisher, app_event_listener
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import TransactionPhase, is_transaction_active, transactional
from tests.support.backend_matrix import RelationalBackend


class PhaseInvoice(Base):
    __tablename__ = "wp09_phase_invoice"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    number: Mapped[str] = mapped_column(String(32))


@repository
class PhaseInvoiceRepository(Repository[PhaseInvoice, int]):
    pass


@dataclass(frozen=True)
class InvoiceIssued:
    number: str


URL: dict[str, str] = {}


async def _committed_numbers() -> list[str]:
    engine = create_async_engine(URL["url"], poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT number FROM wp09_phase_invoice ORDER BY id"))
            return [row[0] for row in rows.all()]
    finally:
        await engine.dispose()


@service
class Mailer:
    """Records which listener ran, whether a transaction was active, and what had committed by then."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool, list[str]]] = []

    async def _record(self, listener: str, event: InvoiceIssued) -> None:
        self.calls.append((listener, event.number, is_transaction_active(), await _committed_numbers()))

    @app_event_listener
    async def inline(self, event: InvoiceIssued) -> None:
        await self._record("inline", event)

    @app_event_listener(phase=TransactionPhase.BEFORE_COMMIT)
    async def before_commit(self, event: InvoiceIssued) -> None:
        await self._record("before_commit", event)

    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def send_mail(self, event: InvoiceIssued) -> None:
        await self._record("after_commit", event)

    @app_event_listener(phase=TransactionPhase.AFTER_ROLLBACK)
    async def compensate(self, event: InvoiceIssued) -> None:
        await self._record("after_rollback", event)

    @app_event_listener(phase=TransactionPhase.AFTER_COMPLETION)
    async def audit(self, event: InvoiceIssued) -> None:
        await self._record("after_completion", event)

    def ran(self) -> list[str]:
        return [listener for listener, *_ in self.calls]

    def call(self, listener: str) -> tuple[str, str, bool, list[str]]:
        return next(call for call in self.calls if call[0] == listener)


@service
class Billing:
    def __init__(self, invoices: PhaseInvoiceRepository, events: ApplicationEventPublisher) -> None:
        self.invoices = invoices
        self.events = events

    @transactional
    async def issue(self, number: str, *, fail: bool = False) -> None:
        await self.invoices.save(PhaseInvoice(number=number))
        await self.events.publish(InvoiceIssued(number))
        if fail:
            raise RuntimeError("the ledger refused the invoice")


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    URL["url"] = backend.url
    await backend.create_tables(PhaseInvoice)
    ctx = ApplicationContext(backend.config())
    for bean in (RelationalAutoConfiguration, PhaseInvoiceRepository, Mailer, Billing):
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


async def test_each_phase_runs_where_it_belongs_when_the_unit_commits(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await ctx.get_bean(Billing).issue("F-1")
        mailer = ctx.get_bean(Mailer)

        assert mailer.ran() == ["inline", "before_commit", "after_commit", "after_completion"]
        assert mailer.call("inline")[2:] == (True, [])  # inside the transaction, nothing committed yet
        assert mailer.call("before_commit")[2:] == (True, [])
        assert mailer.call("after_commit")[2:] == (False, ["F-1"])  # after the commit, outside it
        assert mailer.call("after_completion")[2:] == (False, ["F-1"])
    finally:
        await ctx.stop()


async def test_after_commit_listeners_do_not_run_when_the_unit_rolls_back(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        with pytest.raises(RuntimeError, match="ledger refused"):
            await ctx.get_bean(Billing).issue("F-2", fail=True)
        mailer = ctx.get_bean(Mailer)

        assert mailer.ran()[0] == "inline"
        assert sorted(mailer.ran()[1:]) == ["after_completion", "after_rollback"]
        assert mailer.call("after_rollback")[2:] == (False, [])
        assert await _committed_numbers() == []
    finally:
        await ctx.stop()


async def test_outside_a_transaction_every_phase_but_after_rollback_runs_at_once(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        await ctx.get_bean(ApplicationEventPublisher).publish(InvoiceIssued("F-3"))
        mailer = ctx.get_bean(Mailer)
        assert sorted(mailer.ran()) == ["after_commit", "after_completion", "before_commit", "inline"]
        assert all(active is False for _listener, _number, active, _rows in mailer.calls)
    finally:
        await ctx.stop()


def test_the_phase_is_recorded_on_the_listener() -> None:
    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def listener(event: Any) -> None:
        del event

    @app_event_listener
    async def plain(event: Any) -> None:
        del event

    assert listener.__pyfly_app_event_listener__ is True  # type: ignore[attr-defined]
    assert listener.__pyfly_event_phase__ is TransactionPhase.AFTER_COMMIT  # type: ignore[attr-defined]
    assert plain.__pyfly_event_phase__ is None  # type: ignore[attr-defined]
