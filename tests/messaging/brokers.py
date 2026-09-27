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
"""In-memory Kafka and RabbitMQ that keep the state the listener container is judged on.

They are not offset-less iterators: :class:`FakeKafkaCluster` keeps partitioned logs, each consumer's
position (advanced when a record is handed out, as aiokafka does) and each group's committed offsets,
and a consumer created with ``enable_auto_commit=True`` commits its position when it stops, as
aiokafka's close does. :class:`FakeAmqpBroker` keeps queues, a channel's unacknowledged deliveries and
its ``basic.qos`` prefetch, delivers each message in a task of its own (as aiormq does), requeues what a
closed channel left unacknowledged, and routes a publish only to the queues bound to its key (an
unroutable publish on an ``on_return_raises`` channel raises).
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiokafka import TopicPartition  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------------------------------------


class ConsumerStoppedError(Exception):
    """What aiokafka raises from ``getmany`` on a stopped consumer (matched by name)."""


class IllegalStateError(Exception):
    """A commit on a partition the consumer does not own."""


@dataclass
class FakeRecord:
    topic: str
    partition: int
    offset: int
    value: bytes
    key: bytes | None = None
    headers: list[tuple[str, bytes]] = field(default_factory=list)


class FakeKafkaCluster:
    """Partitioned logs, and the committed offset of every (group, partition)."""

    def __init__(self, partitions: int = 1) -> None:
        self.partitions = partitions
        self.logs: dict[TopicPartition, list[FakeRecord]] = {}
        self.committed: dict[tuple[str, TopicPartition], int] = {}
        self.consumers: list[FakeKafkaConsumer] = []
        self.producers: list[FakeKafkaProducer] = []
        self._appended = asyncio.Event()

    def partitions_of(self, topic: str) -> list[TopicPartition]:
        tps = [TopicPartition(topic, partition) for partition in range(self.partitions)]
        for tp in tps:
            self.logs.setdefault(tp, [])
        return tps

    def append(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        partition: int = 0,
    ) -> FakeRecord:
        self.partitions_of(topic)
        log = self.logs[TopicPartition(topic, partition)]
        record = FakeRecord(topic, partition, len(log), value, key, list(headers or []))
        log.append(record)
        self._appended.set()
        self._appended = asyncio.Event()
        return record

    def records(self, topic: str) -> list[FakeRecord]:
        return [record for tp in self.partitions_of(topic) for record in self.logs[tp]]

    def committed_offset(self, group: str, topic: str, partition: int = 0) -> int | None:
        return self.committed.get((group, TopicPartition(topic, partition)))

    def log_end(self, topic: str, partition: int = 0) -> int:
        return len(self.logs.get(TopicPartition(topic, partition), []))

    async def wait_for_append(self, timeout: float) -> None:
        event = self._appended
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return

    def consumer(self, **settings: Any) -> FakeKafkaConsumer:
        consumer = FakeKafkaConsumer(self, **settings)
        self.consumers.append(consumer)
        return consumer

    def producer(self, **settings: Any) -> FakeKafkaProducer:
        producer = FakeKafkaProducer(self, **settings)
        self.producers.append(producer)
        return producer


class FakeKafkaProducer:
    def __init__(self, cluster: FakeKafkaCluster, **settings: Any) -> None:
        self.cluster = cluster
        self.settings = settings
        self.started = False
        #: Raise this many times on the next sends (a broker that is briefly away).
        self.failures = 0
        self.failed_sends = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_and_wait(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        partition: int | None = None,
    ) -> FakeRecord:
        if not self.started:
            raise RuntimeError("producer is not started")
        if self.failures > 0:
            self.failures -= 1
            self.failed_sends += 1
            raise ConnectionError("broker away")
        return self.cluster.append(topic, value or b"", key=key, headers=headers, partition=partition or 0)


class FakeKafkaConsumer:
    """One group member that owns every partition of its topics (no partition split)."""

    def __init__(
        self,
        cluster: FakeKafkaCluster,
        *,
        bootstrap_servers: str | None = None,
        group_id: str | None = None,
        enable_auto_commit: bool = True,
        auto_offset_reset: str = "latest",
        **settings: Any,
    ) -> None:
        self.cluster = cluster
        self.group_id = group_id
        self.enable_auto_commit = enable_auto_commit
        self.auto_offset_reset = auto_offset_reset
        self.settings = settings
        self.topics: list[str] = []
        self.listener: Any = None
        self.positions: dict[TopicPartition, int] = {}
        self.paused_partitions: set[TopicPartition] = set()
        self.started = False
        self.closed = False
        self.commits: list[dict[TopicPartition, int]] = []
        self.seeks: list[tuple[TopicPartition, int]] = []

    def subscribe(self, topics: list[str], listener: Any = None) -> None:
        self.topics = list(topics)
        self.listener = listener

    async def start(self) -> None:
        self.started = True
        for topic in self.topics:
            for tp in self.cluster.partitions_of(topic):
                committed = self.cluster.committed.get((self.group_id, tp)) if self.group_id else None
                if committed is not None:
                    self.positions[tp] = committed
                else:
                    self.positions[tp] = 0 if self.auto_offset_reset == "earliest" else self.cluster.log_end(tp.topic)
        if self.listener is not None:
            await self.listener.on_partitions_assigned(set(self.positions))

    def assignment(self) -> set[TopicPartition]:
        return set(self.positions)

    async def getmany(self, timeout_ms: int = 0, max_records: int | None = None) -> dict[TopicPartition, list[Any]]:
        if self.closed:
            raise ConsumerStoppedError()
        batch = self._fetch(max_records)
        if batch or timeout_ms <= 0:
            return batch
        await self.cluster.wait_for_append(timeout_ms / 1000)
        if self.closed:
            raise ConsumerStoppedError()
        return self._fetch(max_records)

    def _fetch(self, max_records: int | None) -> dict[TopicPartition, list[Any]]:
        batch: dict[TopicPartition, list[Any]] = {}
        budget = max_records or 1_000_000
        for tp, position in self.positions.items():
            if tp in self.paused_partitions or budget <= 0:
                continue
            records = self.cluster.logs[tp][position : position + budget]
            if records:
                batch[tp] = records
                self.positions[tp] = position + len(records)  # handed out: consumed, as aiokafka counts it
                budget -= len(records)
        return batch

    async def commit(self, offsets: dict[TopicPartition, int] | None = None) -> None:
        if self.group_id is None:
            raise RuntimeError("Requires group_id")
        offsets = dict(self.positions) if offsets is None else dict(offsets)
        for tp in offsets:
            if tp not in self.positions:
                raise IllegalStateError(f"Partition {tp} is not assigned")
        self.commits.append(offsets)
        for tp, offset in offsets.items():
            self.cluster.committed[(self.group_id, tp)] = offset

    def seek(self, tp: TopicPartition, offset: int) -> None:
        if tp not in self.positions:
            raise IllegalStateError(f"No current assignment for partition {tp}")
        self.seeks.append((tp, offset))
        self.positions[tp] = offset

    def pause(self, *tps: TopicPartition) -> None:
        self.paused_partitions.update(tps)

    def resume(self, *tps: TopicPartition) -> None:
        self.paused_partitions.difference_update(tps)

    def paused(self) -> set[TopicPartition]:
        return set(self.paused_partitions)

    async def revoke(self) -> None:
        """What a rebalance does first: the listener hears every partition revoked, then none is owned."""
        revoked = set(self.positions)
        if self.listener is not None:
            await self.listener.on_partitions_revoked(revoked)
        self.positions.clear()

    async def stop(self) -> None:
        if self.closed:
            return
        if self.enable_auto_commit and self.group_id is not None and self.positions:
            await self.commit()  # aiokafka's close-time autocommit of the consumed positions
        self.closed = True
        self.started = False


# ---------------------------------------------------------------------------------------------------------
# RabbitMQ
# ---------------------------------------------------------------------------------------------------------


class DeliveryError(Exception):
    """What aio-pika raises for an unroutable publish on an ``on_return_raises`` channel."""


class ChannelInvalidStateError(Exception):
    """An acknowledgement on a closed channel."""


@dataclass
class StoredMessage:
    body: bytes
    headers: dict[str, Any] = field(default_factory=dict)
    message_id: str | None = None
    correlation_id: str | None = None
    content_type: str | None = None
    redelivered: bool = False


_TAGS = itertools.count(1)


class FakeAmqpBroker:
    """Direct exchanges, durable queues and the outcome of every delivery."""

    def __init__(self) -> None:
        self.bindings: dict[str, dict[str, set[str]]] = {"": {}}
        self.queues: dict[str, deque[StoredMessage]] = {}
        self.queue_arguments: dict[str, dict[str, Any] | None] = {}
        self.channels: list[FakeAmqpChannel] = []
        self.connections: list[FakeAmqpConnection] = []
        #: (outcome, queue, body, attempt header): outcome is ack, requeue or reject.
        self.outcomes: list[tuple[str, str, bytes, Any]] = []

    async def connect(self, url: str) -> FakeAmqpConnection:
        connection = FakeAmqpConnection(self, url)
        self.connections.append(connection)
        return connection

    def declare_queue(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        self.queues.setdefault(name, deque())
        self.queue_arguments.setdefault(name, arguments)

    def route(self, exchange: str, routing_key: str) -> set[str]:
        if exchange == "":
            return {routing_key} if routing_key in self.queues else set()
        return set(self.bindings.get(exchange, {}).get(routing_key, set()))

    def put(self, queue: str, message: StoredMessage, *, head: bool = False) -> None:
        if head:
            self.queues[queue].appendleft(message)
        else:
            self.queues[queue].append(message)
        for channel in list(self.channels):
            channel.dispatch()

    def publish_to(self, exchange: str, routing_key: str, body: bytes, headers: dict[str, Any] | None = None) -> int:
        """Publish from outside the application (a producer service); returns how many queues took it."""
        queues = self.route(exchange, routing_key)
        for queue in queues:
            self.put(queue, StoredMessage(body=body, headers=dict(headers or {})))
        return len(queues)

    def bodies(self, queue: str) -> list[bytes]:
        return [message.body for message in self.queues.get(queue, ())]

    def messages(self, queue: str) -> list[StoredMessage]:
        return list(self.queues.get(queue, ()))


class FakeAmqpConnection:
    def __init__(self, broker: FakeAmqpBroker, url: str) -> None:
        self.broker = broker
        self.url = url
        self.closed = False
        self.channels: list[FakeAmqpChannel] = []

    async def channel(self, publisher_confirms: bool = True, on_return_raises: bool = False) -> FakeAmqpChannel:
        channel = FakeAmqpChannel(self.broker, on_return_raises=on_return_raises)
        self.channels.append(channel)
        self.broker.channels.append(channel)
        return channel

    async def close(self) -> None:
        for channel in list(self.channels):
            await channel.close()
        self.closed = True


class FakeExchange:
    def __init__(self, broker: FakeAmqpBroker, channel: FakeAmqpChannel, name: str) -> None:
        self.broker = broker
        self.channel = channel
        self.name = name
        self.published: list[tuple[Any, str]] = []

    async def publish(self, message: Any, routing_key: str, *, mandatory: bool = True, **_: Any) -> None:
        if self.channel.closed:
            raise ChannelInvalidStateError("channel closed")
        if self.channel.publish_failures > 0:
            self.channel.publish_failures -= 1
            raise ConnectionError("broker away")
        queues = self.broker.route(self.name, routing_key)
        if not queues and mandatory and self.channel.on_return_raises:
            raise DeliveryError(f"unroutable: exchange={self.name!r} routing_key={routing_key!r}")
        self.published.append((message, routing_key))
        for queue in queues:
            self.broker.put(
                queue,
                StoredMessage(
                    body=message.body,
                    headers=dict(message.headers or {}),
                    message_id=message.message_id,
                    correlation_id=message.correlation_id,
                    content_type=message.content_type,
                ),
            )


class FakeQueue:
    def __init__(self, broker: FakeAmqpBroker, channel: FakeAmqpChannel, name: str) -> None:
        self.broker = broker
        self.channel = channel
        self.name = name

    async def bind(self, exchange: FakeExchange, routing_key: str) -> None:
        self.broker.bindings.setdefault(exchange.name, {}).setdefault(routing_key, set()).add(self.name)

    async def consume(self, callback: Callable[[Any], Awaitable[Any]], no_ack: bool = False) -> str:
        assert not no_ack, "the listener container acknowledges manually"
        tag = f"ctag-{next(_TAGS)}"
        self.channel.consumers[tag] = (self.name, callback)
        self.channel.dispatch()
        return tag

    async def cancel(self, tag: str) -> None:
        self.channel.consumers.pop(tag, None)


class FakeIncomingMessage:
    def __init__(self, channel: FakeAmqpChannel, queue: str, stored: StoredMessage, tag: int) -> None:
        self.channel = channel
        self.queue = queue
        self.stored = stored
        self.delivery_tag = tag
        self.body = stored.body
        self.headers = dict(stored.headers)
        self.message_id = stored.message_id
        self.correlation_id = stored.correlation_id
        self.content_type = stored.content_type
        self.content_encoding = None
        self.priority = None
        self.reply_to = None
        self.timestamp = None
        self.type = None
        self.app_id = None
        self.redelivered = stored.redelivered

    async def ack(self) -> None:
        self.channel.settle(self, "ack")

    async def reject(self, requeue: bool = False) -> None:
        self.channel.settle(self, "requeue" if requeue else "reject")

    async def nack(self, requeue: bool = True) -> None:
        self.channel.settle(self, "requeue" if requeue else "reject")


class FakeAmqpChannel:
    """A channel: its prefetch bounds its unacknowledged deliveries; closing it requeues them."""

    def __init__(self, broker: FakeAmqpBroker, *, on_return_raises: bool) -> None:
        self.broker = broker
        self.on_return_raises = on_return_raises
        self.prefetch = 0
        self.qos_calls: list[int] = []
        self.consumers: dict[str, tuple[str, Callable[[Any], Awaitable[Any]]]] = {}
        self.unacked: dict[int, FakeIncomingMessage] = {}
        self.closed = False
        self.publish_failures = 0
        self.max_unacked = 0
        self.default_exchange = FakeExchange(broker, self, "")
        self.tasks: set[asyncio.Task[Any]] = set()
        self._tags = itertools.count(1)

    async def set_qos(self, prefetch_count: int = 0, **_: Any) -> None:
        self.prefetch = prefetch_count
        self.qos_calls.append(prefetch_count)

    async def declare_exchange(self, name: str, kind: Any = None, durable: bool = False) -> FakeExchange:
        self.broker.bindings.setdefault(name, {})
        return FakeExchange(self.broker, self, name)

    async def declare_queue(
        self, name: str, durable: bool = False, arguments: dict[str, Any] | None = None, **_: Any
    ) -> FakeQueue:
        self.broker.declare_queue(name, arguments)
        return FakeQueue(self.broker, self, name)

    def dispatch(self) -> None:
        if self.closed:
            return
        for queue, callback in list(self.consumers.values()):
            pending = self.broker.queues.get(queue)
            while pending and (self.prefetch == 0 or len(self.unacked) < self.prefetch):
                stored = pending.popleft()
                message = FakeIncomingMessage(self, queue, stored, next(self._tags))
                self.unacked[message.delivery_tag] = message
                self.max_unacked = max(self.max_unacked, len(self.unacked))
                task = asyncio.get_running_loop().create_task(callback(message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    def settle(self, message: FakeIncomingMessage, outcome: str) -> None:
        if self.closed:
            raise ChannelInvalidStateError("channel closed")
        if self.unacked.pop(message.delivery_tag, None) is None:
            raise RuntimeError(f"delivery {message.delivery_tag} is already settled")
        self.broker.outcomes.append(
            (outcome, message.queue, message.body, message.headers.get("x-pyfly-delivery-attempt"))
        )
        if outcome == "requeue":
            message.stored.redelivered = True
            self.broker.put(message.queue, message.stored, head=True)
        self.dispatch()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.consumers.clear()
        if self in self.broker.channels:
            self.broker.channels.remove(self)
        for message in sorted(self.unacked.values(), key=lambda m: m.delivery_tag, reverse=True):
            message.stored.redelivered = True
            self.broker.put(message.queue, message.stored, head=True)
        self.unacked.clear()
