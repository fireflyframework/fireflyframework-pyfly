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
"""The outbox store contract: what every :class:`~pyfly.eda.ports.outbox.OutboxStore` adapter does, on its
real backend.

The relay, the ``database``/``postgres`` buses, the forwarding relay and the event-sourcing outbox type against
the port only; this suite is what they rely on. An adapter runs it by subclassing :class:`OutboxStoreContract`
in a test module and defining the ``outbox_harness`` fixture, which yields an :class:`OutboxStoreHarness` for a
backend of its own (the SQL store runs it on every relational lane, ``test_sql_outbox_store_contract.py``)::

    @pytest.fixture
    async def outbox_harness(mongo_backend: MongoBackend) -> AsyncIterator[OutboxStoreHarness]:
        client, manager = ...
        yield OutboxStoreHarness(
            new_store=lambda clock: started(MongoOutboxStore(client, clock=clock)),
            unit=lambda: TransactionTemplate(manager).transaction(),
            name="mongo",
        )


    class TestMongoOutboxStore(OutboxStoreContract):
        pass

Every test reads and writes through the port alone. The stores of one test share a :class:`ManualClock` (the
nodes of a cluster agree on the time), so leases, due times and retention are driven by the test, never by a
sleep; its instants are whole milliseconds, which every backend stores exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pyfly.data.transaction import detached
from pyfly.eda.dlq import EdaDeadLetterEntry
from pyfly.eda.ports.outbox import (
    ADDRESSED_DESTINATION_PREFIX,
    Delivery,
    OutboxStore,
    PendingDelivery,
    PruneResult,
    Retention,
    StartPosition,
)
from pyfly.eda.types import EventEnvelope


class ManualClock:
    """A clock the test moves: it starts at the current second, and only :meth:`advance` changes it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        """Move the clock *seconds* ahead (whole milliseconds) and return the new instant."""
        self._now += timedelta(milliseconds=round(seconds * 1000))
        return self._now


@dataclass
class OutboxStoreHarness:
    """What an adapter gives the contract suite for one test, on a backend of its own.

    - *new_store*: a started store on the test's backend, with *clock*; call it again for another node of the
      cluster (the same data, a store object of its own);
    - *unit*: a business unit of work on the store's datasource (``TransactionTemplate(...).transaction()``):
      an append inside it commits or rolls back with it;
    - *name*: the lane, for messages.
    """

    new_store: Callable[[ManualClock], Awaitable[OutboxStore]]
    unit: Callable[[], contextlib.AbstractAsyncContextManager[Any]]
    name: str


class PortOnlyStore:
    """An outbox store that is nothing but the port: it forwards each method of
    :class:`~pyfly.eda.ports.outbox.OutboxStore` to a real store and has no other attribute.

    Code typed against the port runs on it; code that reaches for an adapter's own methods (an engine, a
    dialect) fails. The tests of the relay, the buses and the event-sourcing outbox use it to prove they depend
    on the port alone."""

    def __init__(self, store: OutboxStore) -> None:
        self._store = store

    async def start(self) -> None:
        await self._store.start()

    async def stop(self) -> None:
        await self._store.stop()

    def now(self) -> datetime:
        return self._store.now()

    async def append(
        self, envelope: EventEnvelope, *, groups: Sequence[str] | None = None, include: Sequence[str] = ()
    ) -> int:
        return await self._store.append(envelope, groups=groups, include=include)

    async def register(
        self,
        group: str,
        destinations: Sequence[str] | None,
        *,
        start: StartPosition | str = StartPosition.LATEST,
    ) -> bool:
        return await self._store.register(group, destinations, start=start)

    async def unregister(self, group: str) -> int:
        return await self._store.unregister(group)

    async def claim(self, group: str, *, limit: int, lease: timedelta, owner: str) -> list[Delivery]:
        return await self._store.claim(group, limit=limit, lease=lease, owner=owner)

    async def complete(self, deliveries: Sequence[Delivery]) -> int:
        return await self._store.complete(deliveries)

    async def settle(
        self,
        delivery: Delivery,
        *,
        done: Iterable[str] = (),
        retry_at: datetime | None = None,
        error: str | None = None,
        dead: Sequence[tuple[str, BaseException]] = (),
    ) -> bool:
        return await self._store.settle(delivery, done=done, retry_at=retry_at, error=error, dead=dead)

    async def extend(self, deliveries: Sequence[Delivery], *, until: datetime) -> list[Delivery]:
        return await self._store.extend(deliveries, until=until)

    async def release(self, deliveries: Sequence[Delivery]) -> int:
        return await self._store.release(deliveries)

    async def pending(self, group: str, *, limit: int = 1000) -> list[PendingDelivery]:
        return await self._store.pending(group, limit=limit)

    async def dead_letters(self, group: str | None = None, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        return await self._store.dead_letters(group, limit=limit)

    async def prune(self, retention: Retention) -> PruneResult:
        return await self._store.prune(retention)


def _event(event_type: str, n: Any, destination: str = "orders", **headers: str) -> EventEnvelope:
    return EventEnvelope(event_type=event_type, payload={"n": n}, destination=destination, headers=dict(headers))


def _numbers(items: Iterable[PendingDelivery | Delivery]) -> list[Any]:
    return [item.envelope.payload["n"] for item in items]


LEASE = timedelta(minutes=5)


class OutboxStoreContract:
    """The contract every outbox store adapter passes (see the module documentation).

    Subclass it as ``Test<Adapter>OutboxStore`` and define the ``outbox_harness`` fixture.
    """

    # -- appending ------------------------------------------------------------------------------------------------

    async def test_an_append_commits_and_rolls_back_with_the_unit(self, outbox_harness: OutboxStoreHarness) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        await store.register("billing", ["orders"])

        with pytest.raises(RuntimeError, match="payment declined"):
            async with outbox_harness.unit():
                await store.append(_event("order.placed", 1))
                raise RuntimeError("payment declined")
        assert await store.pending("billing") == []

        event = EventEnvelope(
            event_type="order.placed",
            payload={"n": 2, "items": [{"sku": "caña", "qty": 1}], "total": 9.5, "gift": None},
            destination="orders",
            headers={"x-trace": "t-1"},
        )
        async with outbox_harness.unit():
            outbox_id = await store.append(event)
        (pending,) = await store.pending("billing")
        assert pending.outbox_id == outbox_id
        assert pending.attempts == 0
        assert pending.last_error is None
        assert pending.available_at == clock()
        stored = pending.envelope
        assert (stored.event_id, stored.event_type, stored.destination) == (
            event.event_id,
            "order.placed",
            "orders",
        )
        assert stored.payload == event.payload
        assert stored.headers == {"x-trace": "t-1"}
        assert stored.timestamp == clock()

    async def test_an_append_is_seen_elsewhere_only_once_its_unit_commits(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        other_node = await outbox_harness.new_store(clock)
        await store.register("billing", ["orders"])

        async with outbox_harness.unit():
            await store.append(_event("order.placed", 1))
            assert _numbers(await store.pending("billing")) == [1]  # the unit sees its own write
            assert await detached(other_node.pending("billing")) == []  # nobody else does yet
        assert _numbers(await other_node.pending("billing")) == [1]

    async def test_outbox_ids_follow_the_publication_order(self, outbox_harness: OutboxStoreHarness) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("g", None)
        ids = [await store.append(_event("e", n)) for n in range(5)]
        assert ids == sorted(ids) and len(set(ids)) == 5
        assert [p.outbox_id for p in await store.pending("g")] == ids

    async def test_an_event_is_owed_to_the_registered_and_included_groups_each_once(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("orders-only", ["orders"])
        await store.register("everything", None)
        await store.register("payments-only", ["payments"])
        await store.register("both", ["*", "orders"])  # what two nodes of a group registering at once leave

        await store.append(_event("order.placed", 1), include=["own", "both", "own"])

        for group in ("orders-only", "everything", "both", "own"):
            assert _numbers(await store.pending(group)) == [1], group
        assert await store.pending("payments-only") == []

    async def test_an_append_to_named_groups_consults_no_registration(self, outbox_harness: OutboxStoreHarness) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("registered", None)

        await store.append(_event("e", 1), groups=["named", "named"], include=["included"])
        await store.append(_event("e", 2), groups=[])

        assert _numbers(await store.pending("named")) == [1]
        assert _numbers(await store.pending("included")) == [1]
        assert await store.pending("registered") == []

    # -- consumer groups -----------------------------------------------------------------------------------------

    async def test_a_registration_reports_a_new_group_once_and_sets_its_destinations(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        store = await outbox_harness.new_store(ManualClock())
        assert await store.register("g", ["orders", "payments"]) is True
        assert await store.register("g", ["payments", "orders"]) is False
        assert await store.register("g", ["payments"]) is False  # the destinations become exactly these

        await store.append(_event("order.placed", "order"))
        await store.append(_event("payment.taken", "payment", destination="payments"))
        assert _numbers(await store.pending("g")) == ["payment"]

    async def test_a_new_group_starts_at_the_latest_event_unless_told_the_earliest(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("first", None)
        await store.append(_event("old", "old"))
        addressed = f"{ADDRESSED_DESTINATION_PREFIX}audit"
        await store.append(_event("stored", "addressed", destination=addressed), groups=[addressed])

        assert await store.register("latest", ["orders"]) is True
        assert await store.register("earliest", ["orders"], start="earliest") is True
        assert await store.register("everything-earliest", None, start=StartPosition.EARLIEST) is True
        await store.append(_event("new", "new"))

        assert _numbers(await store.pending("latest")) == ["new"]
        assert _numbers(await store.pending("earliest")) == ["old", "new"]
        # Every destination, but not the events owed only to the groups their append named.
        assert _numbers(await store.pending("everything-earliest")) == ["old", "new"]

    async def test_a_group_registered_again_earliest_is_not_given_the_backlog_twice(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("first", None)
        await store.append(_event("old", "old"))
        nodes = [await outbox_harness.new_store(ManualClock()) for _ in range(3)]

        results = [await node.register("fresh", ["orders"], start="earliest") for node in nodes]

        assert results == [True, False, False]
        assert _numbers(await store.pending("fresh")) == ["old"]

    async def test_an_unregistered_group_is_owed_nothing_any_more(self, outbox_harness: OutboxStoreHarness) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("g", None)
        await store.append(_event("e", 1))
        await store.append(_event("e", 2))

        assert await store.unregister("g") == 2
        await store.append(_event("e", 3))
        assert await store.pending("g") == []
        assert await store.unregister("g") == 0

    # -- claiming --------------------------------------------------------------------------------------------------

    async def test_a_claim_takes_what_is_due_in_publication_order_for_a_lease(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        other_node = await outbox_harness.new_store(clock)
        await store.register("g", None)
        for n in range(3):
            await store.append(_event("e", n))
        published = clock()
        clock.advance(1)

        first = await store.claim("g", limit=2, lease=LEASE, owner="node-a")
        assert _numbers(first) == [0, 1]
        for delivery in first:
            assert (delivery.group, delivery.attempts, delivery.last_error) == ("g", 1, None)
            assert delivery.done == frozenset()
            assert delivery.token
            assert delivery.leased_until == clock() + LEASE
            assert delivery.due_at == published
        assert len({delivery.token for delivery in first}) == 1  # one claim

        second = await other_node.claim("g", limit=10, lease=LEASE, owner="node-b")
        assert _numbers(second) == [2]
        assert second[0].token != first[0].token
        assert await store.claim("g", limit=10, lease=LEASE, owner="node-a") == []
        assert _numbers(await store.pending("g")) == [0, 1, 2]  # claimed ones are still owed

    async def test_two_nodes_claiming_at_once_never_take_the_same_delivery(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        nodes = [await outbox_harness.new_store(clock) for _ in range(2)]
        await nodes[0].register("workers", None)
        for n in range(40):
            await nodes[0].append(_event("job", n))

        async def drain(node: OutboxStore, owner: str) -> list[int]:
            taken: list[int] = []
            while claimed := await node.claim("workers", limit=3, lease=LEASE, owner=owner):
                taken += [delivery.envelope.payload["n"] for delivery in claimed]
                assert await node.complete(claimed) == len(claimed)
                await asyncio.sleep(0)
            return taken

        first, second = await asyncio.gather(drain(nodes[0], "a"), drain(nodes[1], "b"))

        assert sorted(first + second) == list(range(40))  # none twice, none lost
        assert await nodes[0].pending("workers") == []

    async def test_a_delivery_whose_lease_ended_is_claimed_again_and_the_late_claim_is_fenced(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        dead_node = await outbox_harness.new_store(clock)
        live_node = await outbox_harness.new_store(clock)
        await dead_node.register("g", None)
        await dead_node.append(_event("e", 1))

        (lost,) = await dead_node.claim("g", limit=10, lease=timedelta(seconds=5), owner="dead")
        clock.advance(4)
        assert await live_node.claim("g", limit=10, lease=LEASE, owner="live") == []  # still leased
        clock.advance(2)
        (taken,) = await live_node.claim("g", limit=10, lease=LEASE, owner="live")
        assert (taken.outbox_id, taken.attempts) == (lost.outbox_id, 2)

        assert await dead_node.settle(lost, done={"s"}, retry_at=clock()) is False
        assert await dead_node.settle(lost) is False
        assert await dead_node.complete([lost]) == 0
        assert await dead_node.extend([lost], until=clock() + LEASE) == []
        assert await dead_node.release([lost]) == 0
        assert (await live_node.pending("g"))[0].attempts == 2  # nothing the dead node did landed
        assert await live_node.complete([taken]) == 1
        assert await live_node.pending("g") == []

    async def test_a_delivery_is_not_claimed_before_its_next_attempt_is_due(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        await store.register("g", None)
        await store.append(_event("e", 1))
        (claimed,) = await store.claim("g", limit=10, lease=LEASE, owner="n")

        retry_at = clock() + timedelta(seconds=10)
        assert await store.settle(claimed, done={"a", "b"}, retry_at=retry_at, error="RuntimeError: boom") is True
        (pending,) = await store.pending("g")
        assert (pending.attempts, pending.available_at, pending.last_error) == (1, retry_at, "RuntimeError: boom")

        clock.advance(9)
        assert await store.claim("g", limit=10, lease=LEASE, owner="n") == []
        clock.advance(1)
        (again,) = await store.claim("g", limit=10, lease=LEASE, owner="n")
        assert (again.attempts, again.done, again.last_error) == (2, frozenset({"a", "b"}), "RuntimeError: boom")
        assert again.due_at == retry_at

    # -- settling --------------------------------------------------------------------------------------------------

    async def test_a_settle_without_a_next_attempt_ends_the_delivery_with_its_dead_letters(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        await store.register("mailers", None)
        await store.register("others", None)
        event = _event("poison", "poison", **{"x-trace": "t-2"})
        await store.append(event)
        (claimed,) = await store.claim("mailers", limit=10, lease=LEASE, owner="n")

        settled = await store.settle(
            claimed,
            done={"healthy"},
            retry_at=None,
            error="RuntimeError: cannot",
            dead=[("poison handler", RuntimeError("cannot handle poison"))],
        )

        assert settled is True
        assert await store.pending("mailers") == []
        assert _numbers(await store.pending("others")) == ["poison"]  # another group's delivery is its own
        (letter,) = await store.dead_letters("mailers")
        assert (letter.group, letter.subscription, letter.attempts) == ("mailers", "poison handler", 1)
        assert (letter.error_type, letter.error_message) == ("RuntimeError", "cannot handle poison")
        assert (letter.event.event_id, letter.event.event_type, letter.event.destination) == (
            event.event_id,
            "poison",
            "orders",
        )
        assert letter.event.payload == {"n": "poison"}
        assert letter.event.headers == {"x-trace": "t-2"}
        assert [entry.id for entry in await store.dead_letters()] == [letter.id]
        assert await store.dead_letters("others") == []

    async def test_a_claim_is_completed_at_once(self, outbox_harness: OutboxStoreHarness) -> None:
        store = await outbox_harness.new_store(ManualClock())
        await store.register("g", None)
        for n in range(3):
            await store.append(_event("e", n))
        claimed = await store.claim("g", limit=10, lease=LEASE, owner="n")

        assert await store.complete(claimed) == 3
        assert await store.pending("g") == []
        assert await store.complete(claimed) == 0
        assert await store.complete([]) == 0

    async def test_an_extended_lease_keeps_the_claim(self, outbox_harness: OutboxStoreHarness) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        other_node = await outbox_harness.new_store(clock)
        await store.register("g", None)
        await store.append(_event("e", 1))
        await store.append(_event("e", 2))
        claimed = await store.claim("g", limit=10, lease=timedelta(seconds=5), owner="n")

        until = clock() + timedelta(seconds=60)
        extended = await store.extend(claimed, until=until)
        assert [d.outbox_id for d in extended] == [d.outbox_id for d in claimed]
        assert {d.leased_until for d in extended} == {until}
        clock.advance(30)
        assert await other_node.claim("g", limit=10, lease=LEASE, owner="other") == []
        assert await store.complete(extended) == 2

    async def test_a_release_gives_deliveries_back_where_they_were_unattempted(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        await store.register("g", None)
        await store.append(_event("e", "a"))
        clock.advance(1)
        await store.append(_event("e", "b"))
        clock.advance(1)
        claimed = await store.claim("g", limit=10, lease=LEASE, owner="n")
        due = [delivery.due_at for delivery in claimed]

        assert await store.release(claimed) == 2
        assert await store.release([]) == 0
        assert [(p.attempts, p.available_at) for p in await store.pending("g")] == [(0, due[0]), (0, due[1])]
        again = await store.claim("g", limit=10, lease=LEASE, owner="n")
        assert _numbers(again) == ["a", "b"]
        assert [delivery.attempts for delivery in again] == [1, 1]

    # -- retention --------------------------------------------------------------------------------------------------

    async def test_retention_deletes_what_every_group_handled_and_what_is_past_its_age(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        await store.register("handled", ["orders", "audit"])
        await store.register("idle", ["audit"])
        for n in range(3):
            await store.append(_event("order.placed", n))
        await store.append(_event("login", "login", destination="audit"))
        assert await store.complete(await store.claim("handled", limit=10, lease=LEASE, owner="n")) == 4

        young = await store.prune(Retention(delivered=timedelta(hours=1)))
        assert young == PruneResult()  # nothing is old enough yet

        clock.advance(2 * 3600)
        result = await store.prune(Retention(delivered=timedelta(hours=1), batch_size=2))
        assert result == PruneResult(delivered=3)
        assert await store.register("late", None, start="earliest") is True
        assert _numbers(await store.pending("late")) == ["login"]  # the handled events are gone
        assert _numbers(await store.pending("idle")) == ["login"]

        expired = await store.prune(Retention(delivered=None, max_age=timedelta(hours=1)))
        assert expired == PruneResult(expired=1, undelivered=2)  # owed to "idle" and to "late"
        assert await store.pending("idle") == []
        assert await store.pending("late") == []

    # -- the store itself ----------------------------------------------------------------------------------------------

    async def test_the_store_tells_its_clock_and_starts_and_stops_idempotently(
        self, outbox_harness: OutboxStoreHarness
    ) -> None:
        clock = ManualClock()
        store = await outbox_harness.new_store(clock)
        assert store.now() == clock()
        assert clock.advance(1.5) == store.now()
        await store.start()
        await store.stop()
        await store.stop()
        await store.start()
        await store.register("g", None)
        await store.append(_event("e", 1))
        assert _numbers(await store.pending("g")) == [1]


async def started(store: OutboxStore) -> OutboxStore:
    """*store*, started (a harness's ``new_store`` returns what this does)."""
    await store.start()
    return store


@contextlib.asynccontextmanager
async def stopping(stores: list[OutboxStore]) -> AsyncIterator[list[OutboxStore]]:
    """Stop every store of *stores* when the block ends (a harness keeps the ones it made there)."""
    try:
        yield stores
    finally:
        for store in stores:
            with contextlib.suppress(Exception):
                await store.stop()
