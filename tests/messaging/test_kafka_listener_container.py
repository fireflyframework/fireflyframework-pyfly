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
"""The Kafka listener container on a real application: ``@message_listener`` + ``@transactional`` +
a repository on a SQLite file database, the broker an in-memory cluster that keeps positions and
committed offsets (``brokers.FakeKafkaCluster``).

- C012: auto-commit is off; a failed record is sought back, paused for the back-off and attempted again,
  the records after it wait, and the offset is committed only after the unit committed;
- dead-lettering after the last attempt (and a failed dead-letter publish never commits the record);
- C013: ``stop()`` waits for the record in flight, and one it cancels at the timeout is not committed and
  comes back after a restart;
- a delivery's repository writes commit together or not at all (the container's unit of work).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from pyfly.container.stereotypes import service
from pyfly.context.lifecycle import pre_destroy
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transactional import transactional
from pyfly.kernel.lifecycle import CONSUMER_PHASE, lifecycle_phase
from pyfly.messaging import listener_container
from pyfly.messaging.adapters import kafka as kafka_adapter
from pyfly.messaging.adapters.kafka import KafkaAdapter
from pyfly.messaging.decorators import message_listener
from pyfly.messaging.listener_container import (
    FixedBackOff,
    KafkaListenerContainer,
    ListenerContainerSettings,
    RetryPolicy,
)
from pyfly.messaging.types import Message
from tests.messaging.brokers import FakeKafkaCluster
from tests.messaging.listener_app import (
    Behavior,
    Delivered,
    DeliveredRepository,
    boot,
    broker_bean,
    committed_bodies,
    eventually,
    message_listener_bean,
)
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE)

TOPIC = "orders"
GROUP = "order-service"


def fast(*, max_attempts: int = 3, **settings: Any) -> ListenerContainerSettings:
    retry = RetryPolicy(
        max_attempts=max_attempts, backoff=FixedBackOff(0.01), not_retryable=settings.pop("not_retryable", ())
    )
    return ListenerContainerSettings(retry=retry, poll_timeout=0.05, **settings)


def adapter_on(
    cluster: FakeKafkaCluster, settings: ListenerContainerSettings | None = None, **kwargs: Any
) -> KafkaAdapter:
    return KafkaAdapter(
        "fake:9092",
        settings=settings or fast(),
        auto_offset_reset="earliest",
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
        **kwargs,
    )


async def test_a_failed_record_is_sought_back_and_committed_only_after_its_unit_commits(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    for body in (b"m1", b"m2", b"m3"):
        cluster.append(TOPIC, body)
    behavior = Behavior(fail_first={"m2"})
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster)), message_listener_bean(TOPIC, GROUP, behavior)
    )
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 3, what="offset 3 committed")
    finally:
        await ctx.stop()

    assert await committed_bodies(relational_backend) == ["m1", "m2", "m3"]  # m2's failed attempt rolled back
    assert behavior.seen == ["m1", "m2", "m2", "m3"]  # m3 waited for m2: the partition keeps its order
    [consumer] = cluster.consumers
    assert consumer.enable_auto_commit is False
    assert consumer.seeks and consumer.seeks[0][1] == 1  # sought back to m2
    retried = behavior.messages[2]
    assert isinstance(retried, Message)
    assert (retried.partition, retried.offset, retried.delivery_attempt) == (0, 1, 2)


async def test_the_offset_never_passes_a_record_whose_unit_has_not_committed(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    gate = asyncio.Event()
    behavior = Behavior(gates={"m2": gate})
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster)), message_listener_bean(TOPIC, GROUP, behavior)
    )
    try:
        cluster.append(TOPIC, b"m1")
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="m1 committed")
        cluster.append(TOPIC, b"m2")
        cluster.append(TOPIC, b"m3")
        await behavior.started_event("m2").wait()
        await asyncio.sleep(0.2)  # the old adapter's auto-commit committed the consumed position by now
        assert cluster.committed_offset(GROUP, TOPIC) == 1
        assert await committed_bodies(relational_backend) == ["m1"]
        gate.set()
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 3, what="m2 and m3 committed")
    finally:
        await ctx.stop()
    assert await committed_bodies(relational_backend) == ["m1", "m2", "m3"]


async def test_a_record_that_keeps_failing_goes_to_the_dead_letter_topic_then_is_committed(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"poison", key=b"k", headers=[("x-tenant", b"acme")])
    cluster.append(TOPIC, b"after")
    behavior = Behavior(always_fail={"poison"})
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster)), message_listener_bean(TOPIC, GROUP, behavior)
    )
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 2, what="both records committed")
    finally:
        await ctx.stop()

    assert behavior.attempts["poison"] == 3
    assert await committed_bodies(relational_backend) == ["after"]
    [dead] = cluster.records(f"{TOPIC}.DLT")
    assert (dead.value, dead.key) == (b"poison", b"k")
    headers = {name: value.decode() for name, value in dead.headers}
    assert headers["x-tenant"] == "acme"
    assert headers["x-original-topic"] == TOPIC
    assert headers["x-exception"] == "RuntimeError"
    assert headers["x-dlt-source-offset"] == "0"
    assert headers["x-dlt-source-partition"] == "0"
    assert headers["x-dlt-attempts"] == "3"


async def test_a_failed_dead_letter_publish_leaves_the_record_uncommitted_until_it_succeeds(
    relational_backend: RelationalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kafka_adapter, "DLT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(listener_container, "DEAD_LETTER_RETRY_DELAY", 0.05)
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"poison")
    behavior = Behavior(always_fail={"poison"})
    adapter = adapter_on(cluster)
    ctx = await boot(relational_backend, broker_bean(adapter), message_listener_bean(TOPIC, GROUP, behavior))
    try:
        await eventually(lambda: bool(cluster.producers), what="the producer")
        producer = cluster.producers[0]
        producer.failures = 6  # two rounds of three publish attempts
        await eventually(lambda: producer.failed_sends == 6, what="two failed dead-letter rounds")
        assert cluster.committed_offset(GROUP, TOPIC) is None
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="dead-lettered, then committed")
    finally:
        await ctx.stop()
    assert behavior.attempts["poison"] == 3  # the handler is not run again while the dead letter is retried
    assert [record.value for record in cluster.records(f"{TOPIC}.DLT")] == [b"poison"]


async def test_stop_waits_for_the_record_in_flight_and_commits_it(relational_backend: RelationalBackend) -> None:
    cluster = FakeKafkaCluster()
    gate = asyncio.Event()
    behavior = Behavior(gates={"slow": gate})
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster)), message_listener_bean(TOPIC, GROUP, behavior)
    )
    cluster.append(TOPIC, b"slow")
    await behavior.started_event("slow").wait()
    stopping = asyncio.create_task(ctx.stop())
    await asyncio.sleep(0.1)
    assert not stopping.done()  # the stop waits for the handler inside its transaction
    gate.set()
    await stopping

    assert await committed_bodies(relational_backend) == ["slow"]
    assert cluster.committed_offset(GROUP, TOPIC) == 1


async def test_stop_cancels_a_record_still_running_at_the_timeout_without_committing_it(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    stuck = Behavior(gates={"stuck": asyncio.Event()})
    settings = fast(shutdown_timeout=0.2)
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster, settings)), message_listener_bean(TOPIC, GROUP, stuck)
    )
    cluster.append(TOPIC, b"stuck")
    await stuck.started_event("stuck").wait()
    await ctx.stop()

    assert await committed_bodies(relational_backend) == []  # cancelled inside its unit: rolled back
    assert cluster.committed_offset(GROUP, TOPIC) is None  # and its offset is not committed

    restarted = Behavior()
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster, settings)), message_listener_bean(TOPIC, GROUP, restarted)
    )
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="delivered again and committed")
    finally:
        await ctx.stop()
    assert restarted.attempts["stuck"] == 1
    assert await committed_bodies(relational_backend) == ["stuck"]


ORDER: list[str] = []


@service
class Auditor:
    @pre_destroy
    async def flush(self) -> None:
        ORDER.append("pre_destroy")


async def test_the_consumers_are_drained_before_any_pre_destroy(relational_backend: RelationalBackend) -> None:
    ORDER.clear()
    order = ORDER
    gate = asyncio.Event()
    behavior = Behavior(gates={"slow": gate})
    cluster = FakeKafkaCluster()
    adapter = adapter_on(cluster)
    assert lifecycle_phase(adapter) == CONSUMER_PHASE
    ctx = await boot(relational_backend, broker_bean(adapter), message_listener_bean(TOPIC, GROUP, behavior), Auditor)
    cluster.append(TOPIC, b"slow")
    await behavior.started_event("slow").wait()
    stopping = asyncio.create_task(ctx.stop())
    await asyncio.sleep(0.1)
    order.append("handler released")
    gate.set()
    await stopping
    assert order == ["handler released", "pre_destroy"]
    assert behavior.finished == ["slow"]


ATTEMPTS: list[int] = []


@service
class TwoWrites:
    """No ``@transactional``: its two saves still run in the container's unit."""

    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @message_listener(TOPIC, group=GROUP)
    async def on_message(self, message: Message) -> None:
        ATTEMPTS.append(message.delivery_attempt)
        unit = current_unit_of_work()
        assert unit is not None and not unit.auto  # the container's unit, not an auto unit per call
        await self.repo.save(Delivered(body="first"))
        if message.delivery_attempt == 1:
            raise RuntimeError("the second write fails")
        await self.repo.save(Delivered(body="second"))


async def test_a_delivery_s_repository_writes_commit_together_or_not_at_all(
    relational_backend: RelationalBackend,
) -> None:
    """The failed delivery leaves nothing behind, and the attempt that succeeds commits both rows."""
    ATTEMPTS.clear()
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    ctx = await boot(relational_backend, broker_bean(adapter_on(cluster)), TwoWrites)
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="the record committed")
    finally:
        await ctx.stop()
    assert ATTEMPTS == [1, 2]
    assert await committed_bodies(relational_backend) == ["first", "second"]


JOINED: list[tuple[int | None, int | None]] = []


@service
class InnerWriter:
    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @transactional
    async def save(self, body: str) -> int | None:
        await self.repo.save(Delivered(body=body))
        unit = current_unit_of_work()
        return unit.id if unit else None


@service
class JoiningListener:
    def __init__(self, inner: InnerWriter) -> None:
        self.inner = inner

    @message_listener(TOPIC, group=GROUP)
    async def on_message(self, message: Message) -> None:
        outer = current_unit_of_work()
        inner = await self.inner.save(message.value.decode())
        JOINED.append((outer.id if outer else None, inner))


async def test_the_listener_s_transactional_joins_the_container_unit(relational_backend: RelationalBackend) -> None:
    JOINED.clear()
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"joined")
    ctx = await boot(relational_backend, broker_bean(adapter_on(cluster)), InnerWriter, JoiningListener)
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="the record committed")
    finally:
        await ctx.stop()
    [(outer, inner)] = JOINED
    assert outer is not None and outer == inner
    assert await committed_bodies(relational_backend) == ["joined"]


async def test_listener_options_override_the_container_policy(relational_backend: RelationalBackend) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"poison")
    behavior = Behavior(always_fail={"poison"})
    listener = message_listener_bean(TOPIC, GROUP, behavior, retries=0, dead_letter_topic="orders.failed")
    ctx = await boot(relational_backend, broker_bean(adapter_on(cluster)), listener)
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="dead-lettered and committed")
    finally:
        await ctx.stop()
    assert behavior.attempts["poison"] == 1
    assert [record.value for record in cluster.records("orders.failed")] == [b"poison"]
    assert cluster.records(f"{TOPIC}.DLT") == []


async def test_not_retryable_errors_skip_the_remaining_attempts_but_transient_ones_do_not(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"invalid")
    cluster.append(TOPIC, b"busy")
    behavior = Behavior(
        errors={"invalid": lambda: ValueError("not an order"), "busy": lambda: ConnectionResetError("db restarting")}
    )
    settings = fast(not_retryable=(Exception,))
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(cluster, settings)), message_listener_bean(TOPIC, GROUP, behavior)
    )
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 2, what="both dead-lettered")
    finally:
        await ctx.stop()
    assert behavior.attempts["invalid"] == 1
    assert behavior.attempts["busy"] == 3
    assert [record.value for record in cluster.records(f"{TOPIC}.DLT")] == [b"invalid", b"busy"]


async def test_dead_lettering_can_be_switched_off(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"poison")
    behavior = Behavior(always_fail={"poison"})
    adapter = adapter_on(cluster, dead_letter_suffix=None)
    with caplog.at_level(logging.ERROR, logger="pyfly.messaging.listener_container"):
        ctx = await boot(relational_backend, broker_bean(adapter), message_listener_bean(TOPIC, GROUP, behavior))
        try:
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="skipped and committed")
        finally:
            await ctx.stop()
    assert behavior.attempts["poison"] == 3
    assert cluster.records(f"{TOPIC}.DLT") == []
    assert any("listener_delivery_skipped" in record.getMessage() for record in caplog.records)


async def test_a_consumer_without_a_group_commits_nothing_and_says_so(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m1")
    behavior = Behavior()
    with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
        ctx = await boot(
            relational_backend, broker_bean(adapter_on(cluster)), message_listener_bean(TOPIC, None, behavior)
        )
        try:
            await eventually(lambda: behavior.finished == ["m1"], what="m1 handled")
        finally:
            await ctx.stop()
    assert cluster.committed == {}
    assert any("listener_without_consumer_group" in record.getMessage() for record in caplog.records)


async def test_revoked_partitions_commit_what_is_done_after_the_record_in_flight(
    relational_backend: RelationalBackend,
) -> None:
    """A rebalance waits for the record in flight (bounded), commits it, and the new owner starts after it."""
    await relational_backend.create_tables(Delivered)
    cluster = FakeKafkaCluster()
    gate = asyncio.Event()
    behavior = Behavior(gates={"m1": gate})
    handled: list[str] = []

    async def handler(message: Message) -> None:
        handled.append(message.value.decode())
        if message.value == b"m1":
            await gate.wait()

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False),
        auto_offset_reset="earliest",
    )
    await container.start()
    try:
        cluster.append(TOPIC, b"m1")
        await eventually(lambda: handled == ["m1"], what="m1 in flight")
        [consumer] = cluster.consumers
        revoking = asyncio.create_task(consumer.revoke())
        await asyncio.sleep(0.05)
        assert not revoking.done()  # the revocation waits for the record in flight
        gate.set()
        await revoking
        assert cluster.committed_offset(GROUP, TOPIC) == 1
    finally:
        await container.stop()
    assert behavior.attempts == {}


async def test_a_record_that_fails_while_its_partition_is_revoked_does_not_stop_the_consumer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The seek back to a failed record raises once the partition is gone; the loop logs it and goes on,
    and the new owner gets the record from the committed offset."""
    cluster = FakeKafkaCluster()
    gate = asyncio.Event()
    handled: list[str] = []

    async def handler(message: Message) -> None:
        handled.append(message.value.decode())
        if message.value == b"m1":
            await gate.wait()
            raise RuntimeError("fails after the rebalance began")

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False, shutdown_timeout=0.05),
        auto_offset_reset="earliest",
    )
    await container.start()
    try:
        cluster.append(TOPIC, b"m1")
        await eventually(lambda: handled == ["m1"], what="m1 in flight")
        [consumer] = cluster.consumers
        with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
            await consumer.revoke()  # waits 0.05 s for m1, then takes the partition away
            gate.set()
            await eventually(
                lambda: any("listener_back_off_failed" in r.getMessage() for r in caplog.records), what="the log"
            )
        await asyncio.sleep(0.1)
        assert container.running
        assert cluster.committed_offset(GROUP, TOPIC) is None
    finally:
        await container.stop()


async def test_a_revoked_partition_s_next_record_is_not_started_while_the_rebalance_waits() -> None:
    """The rebalance waits for m1; m2, fetched in the same poll, must not start on this member meanwhile: its
    new owner gets it from the committed offset, and two members would run it at once."""
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m1")
    cluster.append(TOPIC, b"m2")
    gate = asyncio.Event()
    handled: list[str] = []

    async def handler(message: Message) -> None:
        handled.append(message.value.decode())
        if message.value == b"m1":
            await gate.wait()

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False),
        auto_offset_reset="earliest",
    )
    await container.start()
    try:
        await eventually(lambda: handled == ["m1"], what="m1 in flight")
        [consumer] = cluster.consumers
        revoking = asyncio.create_task(consumer.revoke())
        await asyncio.sleep(0.05)
        gate.set()
        await revoking
        await asyncio.sleep(0.05)
    finally:
        await container.stop()
    assert handled == ["m1"]
    assert cluster.committed_offset(GROUP, TOPIC) == 1  # m2 is left to the partition's new owner


async def test_a_cancelled_error_the_handler_raises_itself_is_a_failed_delivery() -> None:
    """A handler that awaits a task someone cancelled gets ``CancelledError`` without being stopped: the
    record is attempted again, and the consumer keeps consuming."""
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m1")
    cluster.append(TOPIC, b"m2")
    attempts: list[tuple[str, int]] = []

    async def handler(message: Message) -> None:
        attempts.append((message.value.decode(), message.delivery_attempt))
        if message.value == b"m1" and message.delivery_attempt == 1:
            side_task = asyncio.create_task(asyncio.sleep(10))
            side_task.cancel()
            await side_task  # raises CancelledError into the handler

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False),
        auto_offset_reset="earliest",
    )
    await container.start()
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 2, what="both committed")
        assert container.running
    finally:
        await container.stop()
    assert attempts == [("m1", 1), ("m1", 2), ("m2", 1)]


async def test_the_loop_fetches_nothing_until_it_is_ready() -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m1")
    handled: list[str] = []
    ready = asyncio.Event()

    async def handler(message: Message) -> None:
        handled.append(message.value.decode())

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False),
        auto_offset_reset="earliest",
        ready=ready,
    )
    await container.start()
    try:
        [consumer] = cluster.consumers
        assert consumer.started  # the consumer joined its group at start
        await asyncio.sleep(0.1)
        assert handled == [] and consumer.positions[next(iter(consumer.positions))] == 0
        ready.set()
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="m1 committed")
    finally:
        await container.stop()
    assert handled == ["m1"]


async def test_a_container_that_is_not_ready_stops_at_once() -> None:
    cluster = FakeKafkaCluster()

    async def handler(message: Message) -> None:
        raise AssertionError("nothing is fetched")

    container: KafkaListenerContainer[Message] = KafkaListenerContainer(
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        convert=kafka_adapter.message_of,
        handler=handler,
        dead_letter=None,
        settings=fast(transactional=False, shutdown_timeout=5.0),
        auto_offset_reset="earliest",
        ready=asyncio.Event(),
    )
    await container.start()
    loop = asyncio.get_running_loop()
    began = loop.time()
    await container.stop()
    assert loop.time() - began < 1.0
    assert not container.running and cluster.consumers[0].closed
