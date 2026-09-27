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
"""``eventsourcing.TransactionalOutbox`` is table-backed and part of the unit of work (F8).

It was a dictionary in the process: an event enqueued by a unit that rolled back was published anyway, a
restart lost what was pending, and nothing was ever removed. SQLite file (foreign keys on), PostgreSQL,
MySQL 8 and MariaDB 11.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.outbox import TransactionalOutbox
from pyfly.messaging.listener_container import FixedBackOff
from tests.support.backend_matrix import RelationalBackend


def _event(aggregate: str, sequence: int = 0) -> StoredEventEnvelope:
    return StoredEventEnvelope(
        aggregate_id=aggregate, aggregate_type="Account", sequence=sequence, event_type="Opened", payload={"n": 1}
    )


async def _eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(300):
        if await condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_an_enqueue_is_part_of_the_unit_of_work(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    published: list[StoredEventEnvelope] = []

    async def publish(envelope: StoredEventEnvelope) -> None:
        published.append(envelope)

    outbox = TransactionalOutbox(publish, datasource=engine, poll_interval_s=0.05)
    await outbox.start()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    try:
        with pytest.raises(RuntimeError):
            async with template.transaction():
                await outbox.enqueue(_event("rolled-back"))
                raise RuntimeError("the append failed")
        async with template.transaction():
            record = await outbox.enqueue(_event("committed", 3))

        async def delivered() -> bool:
            return bool(published) and await outbox.pending() == []

        await _eventually(delivered)
    finally:
        await outbox.stop()

    assert [envelope.aggregate_id for envelope in published] == ["committed"]
    assert published[0].sequence == 3
    assert published[0].payload == {"n": 1}
    assert record.event.aggregate_id == "committed"


async def test_exhausted_events_are_dead_lettered(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    attempts = 0

    async def always_fail(_envelope: StoredEventEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("upstream down")

    outbox = TransactionalOutbox(
        always_fail, datasource=engine, max_attempts=2, poll_interval_s=0.02, backoff=FixedBackOff(0.0)
    )
    await outbox.start()
    try:
        record = await outbox.enqueue(_event("acc-1"))

        async def dead() -> bool:
            return bool(await outbox.dead_letters())

        await _eventually(dead)
    finally:
        await outbox.stop()

    assert attempts == 2
    assert await outbox.pending() == []  # excluded from the relay
    letters = await outbox.dead_letters()  # but surfaced for inspection
    assert [(letter.id, letter.attempts, letter.event.aggregate_id) for letter in letters] == [(record.id, 2, "acc-1")]
    assert letters[0].last_error == "RuntimeError: upstream down"


async def test_what_is_pending_survives_a_restart(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    published: list[str] = []

    async def publish(envelope: StoredEventEnvelope) -> None:
        published.append(envelope.aggregate_id)

    first = TransactionalOutbox(publish, datasource=engine)
    await first.start()
    await first.stop()  # the process goes away before its relay published anything
    await first.enqueue(_event("pending-at-shutdown"))
    assert [record.event.aggregate_id for record in await first.pending()] == ["pending-at-shutdown"]

    second = TransactionalOutbox(publish, datasource=engine, poll_interval_s=0.05)
    await second.start()
    try:

        async def delivered() -> bool:
            return published == ["pending-at-shutdown"]

        await _eventually(delivered)
    finally:
        await second.stop()


async def test_outboxes_with_different_names_deliver_apart(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    seen: dict[str, list[str]] = {"mail": [], "audit": []}

    def publisher(name: str) -> Any:
        async def publish(envelope: StoredEventEnvelope) -> None:
            seen[name].append(envelope.aggregate_id)

        return publish

    mail = TransactionalOutbox(publisher("mail"), datasource=engine, name="mail", poll_interval_s=0.05)
    audit = TransactionalOutbox(publisher("audit"), datasource=engine, name="audit", poll_interval_s=0.05)
    await mail.start()
    await audit.start()
    try:
        await mail.enqueue(_event("to-mail"))
        await audit.enqueue(_event("to-audit"))

        async def both() -> bool:
            return seen == {"mail": ["to-mail"], "audit": ["to-audit"]}

        await _eventually(both)
    finally:
        await mail.stop()
        await audit.stop()
