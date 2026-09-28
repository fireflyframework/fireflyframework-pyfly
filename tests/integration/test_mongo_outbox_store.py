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
"""What the MongoDB outbox store does beyond the outbox store contract, on a real replica set.

- The outbox ids come from a counter incremented outside the publishing transactions: concurrent publishing units
  never conflict on it, and the ids grow;
- a rolled-back append leaves no event, and a unit that commits late (a lower id behind delivered higher ones) is
  still delivered: there is no id cursor to skip it;
- a relay process killed in the middle of its lease leaves its delivery to be claimed again once the lease ends;
- two relays on two clients split a group's deliveries and never take one twice;
- a single-command unit of the store's own that fails after its write reports an unknown outcome;
- a standalone server is refused, a read-only unit refuses an append, the indexes are created (or checked), and
  the consumers that share a store each stop it without closing the client.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient, monitoring

from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionTemplate,
    detached,
    track_commits,
)
from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.adapters.mongo_outbox import MongoOutboxCollections, MongoOutboxStore
from pyfly.eda.outbox import OutboxRelay
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.eventsourcing.outbox import TransactionalOutbox
from tests.support.backend_matrix import MongoBackend

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.mongo]

LEASE = timedelta(minutes=5)


def _event(n: Any, destination: str = "orders") -> EventEnvelope:
    return EventEnvelope(event_type="order.placed", payload={"n": n}, destination=destination)


@dataclass
class Mongo:
    backend: MongoBackend
    client: AsyncMongoClient[Any]
    manager: MongoTransactionManager
    clients: list[AsyncMongoClient[Any]]

    def store(self, **options: Any) -> MongoOutboxStore:
        return MongoOutboxStore(self.manager, database=self.backend.database, **options)

    def template(self, **settings: Any) -> TransactionTemplate:
        return TransactionTemplate(self.manager, **settings)

    def another_client(self, **options: Any) -> AsyncMongoClient[Any]:
        """A client of its own (another process's), closed after the test."""
        client: AsyncMongoClient[Any] = AsyncMongoClient(self.backend.url, **options)
        self.clients.append(client)
        return client

    def collection(self, name: str) -> Any:
        return self.client[self.backend.database][name]


@pytest.fixture
async def mongo(mongo_backend: MongoBackend) -> AsyncIterator[Mongo]:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
    env = Mongo(mongo_backend, client, MongoTransactionManager.for_client(client), [client])
    try:
        yield env
    finally:
        for opened in env.clients:
            await opened.close()


# ---------------------------------------------------------------------------------------------------------------
# Outbox ids
# ---------------------------------------------------------------------------------------------------------------


async def test_concurrent_publishing_units_never_conflict_on_the_counter_and_the_ids_grow(mongo: Mongo) -> None:
    store = mongo.store()
    await store.start()
    await store.register("g", None)
    template = mongo.template()
    ids: dict[str, list[int]] = {}

    async def in_a_unit(name: str) -> None:
        async with template.transaction():
            ids[name] = [await store.append(_event(f"{name}-{n}")) for n in range(3)]
            await asyncio.sleep(0.05)  # the units stay open side by side

    async def on_its_own(name: str) -> None:
        ids[name] = [await store.append(_event(f"{name}-{n}")) for n in range(3)]

    await asyncio.gather(*(in_a_unit(f"u{n}") for n in range(8)), *(on_its_own(f"o{n}") for n in range(4)))

    every = [outbox_id for taken in ids.values() for outbox_id in taken]
    assert len(set(every)) == 36
    assert all(taken == sorted(taken) for taken in ids.values())  # each publisher's events in its own order
    pending = await store.pending("g")
    assert [p.outbox_id for p in pending] == sorted(every)
    counter = await mongo.collection(store.collections.counters).find_one({"_id": store.collections.events})
    assert counter is not None and counter["value"] == max(every)


async def test_a_rolled_back_append_leaves_no_event_and_a_late_commit_is_still_delivered(mongo: Mongo) -> None:
    store = mongo.store()
    await store.start()
    await store.register("g", None)
    template = mongo.template()

    with pytest.raises(RuntimeError, match="declined"):
        async with template.transaction():
            rolled_back = await store.append(_event("rolled back"))
            raise RuntimeError("declined")
    assert await mongo.collection(store.collections.events).find_one({"_id": rolled_back}) is None
    assert await mongo.collection(store.collections.deliveries).count_documents({}) == 0

    committed_late = asyncio.Event()
    appended = asyncio.Event()
    late: list[int] = []

    async def slow_unit() -> None:
        async with template.transaction():
            late.append(await store.append(_event("late")))
            appended.set()
            await committed_late.wait()

    unit = asyncio.ensure_future(slow_unit())
    await appended.wait()
    early = await store.append(_event("early"))
    assert early > late[0] > rolled_back  # the late unit holds the lower id
    (claimed,) = await store.claim("g", limit=10, lease=LEASE, owner="n")
    assert claimed.envelope.payload == {"n": "early"}
    assert await store.complete([claimed]) == 1

    committed_late.set()
    await unit
    (claimed,) = await store.claim("g", limit=10, lease=LEASE, owner="n")  # no cursor went past it
    assert (claimed.outbox_id, claimed.envelope.payload) == (late[0], {"n": "late"})


async def test_the_payload_is_kept_as_json_as_the_sql_store_keeps_it(mongo: Mongo) -> None:
    store = mongo.store()
    await store.start()
    await store.register("g", None)
    at = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    await store.append(
        EventEnvelope("order.placed", {"at": at, "amount": Decimal("9.50"), "big": 2**70}, "orders", headers={"h": "1"})
    )

    (pending,) = await store.pending("g")
    assert pending.envelope.payload == {"at": "2026-09-28T12:00:00+00:00", "amount": "9.50", "big": 2**70}
    stored = await mongo.collection(store.collections.events).find_one({})
    assert stored is not None and isinstance(stored["payload"], str)


# ---------------------------------------------------------------------------------------------------------------
# Leases and relays
# ---------------------------------------------------------------------------------------------------------------

_DYING_RELAY = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    from pymongo import AsyncMongoClient

    from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
    from pyfly.eda.outbox import OutboxRelay


    async def main(url, database):
        store = MongoOutboxStore(AsyncMongoClient(url), database=database)
        relay = OutboxRelay(store, group="g", transactional=False, claim_timeout=4.0, handler_timeout=3.0)

        async def dies(envelope):
            os._exit(137)  # the handler runs; the process dies before the delivery is settled

        relay.subscribe("*", dies)
        await relay.run_once()
        sys.exit(3)  # nothing was claimed


    asyncio.run(main(*sys.argv[1:]))
    """
)


async def test_a_relay_killed_mid_lease_leaves_its_delivery_to_be_claimed_again_once_the_lease_ends(
    mongo: Mongo, tmp_path: Path
) -> None:
    store = mongo.store()
    await store.start()
    await store.register("g", None)
    await store.append(_event(1))

    script = tmp_path / "dying_relay.py"
    script.write_text(_DYING_RELAY)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        mongo.backend.url,
        mongo.backend.database,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode == 137, output.decode(errors="replace")[-2000:]
    (held,) = await store.pending("g")
    assert held.attempts == 1

    survivor_store = MongoOutboxStore(mongo.another_client(), database=mongo.backend.database)
    survivor = OutboxRelay(survivor_store, group="g", transactional=False, claim_timeout=4.0, handler_timeout=3.0)
    handled: list[Any] = []

    async def handler(envelope: EventEnvelope) -> None:
        handled.append(envelope.payload["n"])

    survivor.subscribe("*", handler)
    assert await survivor.run_once() == 0  # the dead process's claim holds until its lease ends
    deadline = time.monotonic() + 30
    while await survivor.run_once() == 0:
        assert time.monotonic() < deadline, "the dead process's lease never ended"
        await asyncio.sleep(0.25)

    assert handled == [1]
    assert survivor.counters.delivered == 1
    assert await store.pending("g") == []


async def test_two_relays_on_two_clients_split_the_deliveries_and_never_take_one_twice(mongo: Mongo) -> None:
    publisher = mongo.store()
    await publisher.start()
    relays: list[OutboxRelay] = []
    handled: list[list[int]] = [[], []]
    for index in range(2):
        store = MongoOutboxStore(mongo.another_client(), database=mongo.backend.database)
        relay = OutboxRelay(store, group="workers", transactional=False, batch_size=4, owner=f"node-{index}")
        taken = handled[index]

        async def handler(envelope: EventEnvelope, taken: list[int] = taken) -> None:
            taken.append(envelope.payload["n"])
            await asyncio.sleep(0.01)  # a delivery takes a while: the other relay claims meanwhile

        relay.subscribe("*", handler)
        await relay.register()
        relays.append(relay)
    for n in range(60):
        await publisher.append(_event(n))

    async def drain(relay: OutboxRelay) -> None:
        while await relay.run_once():
            await asyncio.sleep(0)

    await asyncio.gather(*(drain(relay) for relay in relays))

    assert sorted(handled[0] + handled[1]) == list(range(60))  # none twice, none lost
    assert handled[0] and handled[1]  # both took their share
    assert sum(relay.counters.delivered for relay in relays) == 60
    assert await publisher.pending("workers") == []


# ---------------------------------------------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------------------------------------------


class _CancelOn(monitoring.CommandListener):
    """Cancels *task* as the client starts a ``find`` on *collection* after an ``update`` was sent: a claim
    interrupted between its write and its read."""

    def __init__(self, collection: str) -> None:
        self.collection = collection
        self.task: asyncio.Task[Any] | None = None
        self.updated = False

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if self.task is None:
            return
        if event.command_name == "update":
            self.updated = True
        elif self.updated and event.command_name == "find" and event.command.get("find") == self.collection:
            self.task.cancel()
            self.task = None

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


async def test_a_single_command_unit_that_fails_after_its_write_reports_an_unknown_outcome(mongo: Mongo) -> None:
    """A claim runs without a transaction: its ``updateMany`` stands as soon as it ran. Interrupted after it, its
    unit may have written: a commit tracker must hear ``unknown`` (compensate, never retry blindly), not a
    rollback."""
    names = MongoOutboxCollections.named()
    listener = _CancelOn(names.deliveries)
    client = mongo.another_client(event_listeners=[listener])
    store = MongoOutboxStore(client, database=mongo.backend.database)
    await store.start()
    await store.register("g", None)
    await store.append(_event(1))

    async def claim() -> tuple[int, int, int]:
        with track_commits() as commits:
            listener.task = asyncio.current_task()
            with pytest.raises(asyncio.CancelledError):
                await store.claim("g", limit=10, lease=LEASE, owner="n")
            asyncio.current_task().uncancel()  # type: ignore[union-attr]
        return commits.committed, commits.rolled_back, commits.unknown

    assert await detached(claim()) == (0, 0, 1)
    (held,) = await store.pending("g")
    assert held.attempts == 1  # the claim's write stood


async def test_a_standalone_server_is_refused_at_start(mongo_url: str) -> None:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_url)
    try:
        with pytest.raises(IllegalTransactionStateError, match="replica set"):
            await MongoOutboxStore(client, database="pyfly_wp06b_standalone").start()
    finally:
        await client.close()


async def test_a_read_only_unit_refuses_an_append(mongo: Mongo) -> None:
    store = mongo.store()
    await store.start()
    await store.register("g", None)
    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        async with mongo.template(read_only=True).transaction():
            await store.append(_event(1))
    assert await store.pending("g") == []


async def test_start_creates_the_indexes_or_only_checks_them_and_sets_the_counter_past_the_events(mongo: Mongo) -> None:
    checking = mongo.store(create_indexes=False)
    with pytest.raises(IllegalTransactionStateError, match="'created_at' of collection 'pyfly_outbox_events'"):
        await checking.start()

    store = mongo.store()
    await store.start()
    await store.start()  # idempotent
    await checking.start()
    names = store.collections
    for collection, wanted in (
        (names.events, {"_id_", "created_at"}),
        (names.deliveries, {"_id_", "group_outbox_id", "group_available_at", "outbox_id"}),
        (names.consumers, {"_id_", "group_destination", "destination"}),
        (names.dead_letters, {"_id_", "group_failed_at", "failed_at"}),
    ):
        assert set(await mongo.collection(collection).index_information()) == wanted, collection

    first = await store.append(_event(1))
    await mongo.collection(names.counters).delete_many({})  # a counter lost (restored from a backup without it)
    await store.start()
    assert await store.append(_event(2)) > first


async def test_the_consumers_sharing_a_store_each_stop_it_and_the_client_stays_open(mongo: Mongo) -> None:
    store = mongo.store()

    async def publish(envelope: Any) -> None:
        return None

    bus = DatabaseEventBus(store=store, group="billing")
    publisher = TransactionalEventPublisher(InMemoryEventBus(), store, name="memory")
    outbox = TransactionalOutbox(publish, store=store)
    for consumer in (bus, publisher, outbox):
        await consumer.start()
    for consumer in (bus, publisher, outbox):
        await consumer.stop()

    await store.register("g", None)
    await store.append(_event("after the stops"))
    assert [p.envelope.payload for p in await store.pending("g")] == [{"n": "after the stops"}]
    assert (await mongo.client.admin.command("ping"))["ok"] == 1.0
