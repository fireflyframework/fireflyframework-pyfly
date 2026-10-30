# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Managed Kafka subscriptions against an isolated real broker."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from pyfly.messaging.adapters.kafka import KafkaAdapter
from pyfly.messaging.listener_container import FixedBackOff, ListenerContainerSettings, RetryPolicy
from pyfly.messaging.types import Message
from pyfly.testing import requires_docker


async def create_topics(url: str, *names: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=url)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(name=name, num_partitions=1, replication_factor=1) for name in names])
    finally:
        await admin.close()


async def committed(url: str, group: str, topic: str) -> int | None:
    admin = AIOKafkaAdminClient(bootstrap_servers=url)
    await admin.start()
    try:
        offsets = await admin.list_consumer_group_offsets(group)
    finally:
        await admin.close()
    value = offsets.get(TopicPartition(topic, 0))
    return None if value is None else value.offset


@requires_docker
@pytest.mark.asyncio
async def test_managed_drain_commit_restart_and_dead_letter(kafka_url: str) -> None:
    name = f"pyfly-managed-{uuid.uuid4().hex}"
    topic, group, dead = name, f"{name}-group", f"{name}.DLT"
    await create_topics(kafka_url, topic, dead)
    settings = ListenerContainerSettings(retry=RetryPolicy(max_attempts=2, backoff=FixedBackOff(0.05)))
    adapter = KafkaAdapter(bootstrap_servers=kafka_url, settings=settings, auto_offset_reset="earliest")
    entered, release, accepted = asyncio.Event(), asyncio.Event(), []

    async def handle(message: Message) -> None:
        if message.value == b"poison":
            raise ValueError("poison")
        accepted.append((message.value, message.partition, message.offset))
        if message.value == b"first":
            entered.set()
            await release.wait()

    await adapter.start()
    try:
        handle_one = await adapter.subscribe_managed(topic, handle, group)
        await adapter.publish(topic, b"first")
        await asyncio.wait_for(entered.wait(), 30)
        assert await committed(kafka_url, group, topic) is None
        stopping = asyncio.create_task(handle_one.stop())
        await asyncio.sleep(0.1)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 30)
        assert await committed(kafka_url, group, topic) == 1
        handle_two = await adapter.subscribe_managed(topic, handle, group)
        await adapter.publish(topic, b"second")
        await adapter.publish(topic, b"poison")
        consumer = AIOKafkaConsumer(dead, bootstrap_servers=kafka_url, auto_offset_reset="earliest")
        await consumer.start()
        try:
            record = await asyncio.wait_for(consumer.getone(), 30)
            assert record.value == b"poison"
        finally:
            await consumer.stop()
        await handle_two.stop()
        assert accepted == [(b"first", 0, 0), (b"second", 0, 1)]
        assert await committed(kafka_url, group, topic) == 3
    finally:
        release.set()
        await adapter.stop()


@requires_docker
@pytest.mark.asyncio
async def test_managed_stop_timeout_redelivers_uncommitted_record(kafka_url: str) -> None:
    name = f"pyfly-redelivery-{uuid.uuid4().hex}"
    topic, group = name, f"{name}-group"
    await create_topics(kafka_url, topic, f"{name}.DLT")
    adapter = KafkaAdapter(
        bootstrap_servers=kafka_url,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(shutdown_timeout=0.1),
    )
    entered, again = asyncio.Event(), asyncio.Event()
    deliveries = []

    async def hold(message: Message) -> None:
        deliveries.append((message.partition, message.offset))
        entered.set()
        await asyncio.Event().wait()

    async def finish(message: Message) -> None:
        deliveries.append((message.partition, message.offset))
        again.set()

    await adapter.start()
    try:
        handle = await adapter.subscribe_managed(topic, hold, group)
        await adapter.publish(topic, b"pending")
        await asyncio.wait_for(entered.wait(), 30)
        await handle.stop()
        assert await committed(kafka_url, group, topic) is None
        replacement = await adapter.subscribe_managed(topic, finish, group)
        await asyncio.wait_for(again.wait(), 30)
        await replacement.stop()
        assert deliveries == [(0, 0), (0, 0)]
        assert await committed(kafka_url, group, topic) == 1
    finally:
        await adapter.stop()


@requires_docker
@pytest.mark.asyncio
async def test_retained_admission_outage_never_commits_or_dead_letters_before_recovery(kafka_url: str) -> None:
    from pyfly.messaging.listener_container import RetainMessageError

    name = f"pyfly-retained-{uuid.uuid4().hex}"
    topic, group, dead = name, f"{name}-group", f"{name}.DLT"
    await create_topics(kafka_url, topic, dead)
    adapter = KafkaAdapter(
        bootstrap_servers=kafka_url,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(retry=RetryPolicy(max_attempts=1, backoff=FixedBackOff(0.01))),
    )
    attempts = []
    ready, success = asyncio.Event(), asyncio.Event()

    async def handler(message: Message) -> None:
        attempts.append((message.partition, message.offset))
        if not ready.is_set():
            raise RetainMessageError("temporarily unavailable")
        success.set()

    await adapter.start()
    handle = await adapter.subscribe_managed(topic, handler, group)
    try:
        await adapter.publish(topic, b"pending")
        async with asyncio.timeout(30):
            while len(attempts) < 3:
                await asyncio.sleep(0.05)
        assert await committed(kafka_url, group, topic) is None
        consumer = AIOKafkaConsumer(dead, bootstrap_servers=kafka_url, auto_offset_reset="earliest")
        await consumer.start()
        try:
            assert not await consumer.getmany(timeout_ms=500)
        finally:
            await consumer.stop()
        ready.set()
        await asyncio.wait_for(success.wait(), 30)
        await handle.stop()
        assert await committed(kafka_url, group, topic) == 1
        assert set(attempts) == {(0, 0)}
    finally:
        await adapter.stop()


@requires_docker
@pytest.mark.asyncio
async def test_unconfirmed_kafka_drain_keeps_consumer_owned(kafka_url: str) -> None:
    from pyfly.messaging.listener_container import UnconfirmedDrainError

    name = f"pyfly-drain-{uuid.uuid4().hex}"
    topic, group = name, f"{name}-group"
    await create_topics(kafka_url, topic, f"{name}.DLT")
    adapter = KafkaAdapter(
        bootstrap_servers=kafka_url,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(shutdown_timeout=0.01),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(message: Message) -> None:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    await adapter.start()
    handle = await adapter.subscribe_managed(topic, handler, group)
    try:
        await adapter.publish(topic, b"pending")
        await asyncio.wait_for(entered.wait(), 30)
        with pytest.raises(UnconfirmedDrainError):
            await handle.stop()
        assert await committed(kafka_url, group, topic) is None
        with pytest.raises(UnconfirmedDrainError):
            await adapter.subscribe_managed(topic, handler, group)
        assert (topic, group) in adapter._containers
        release.set()
        await handle.stop()
        assert await committed(kafka_url, group, topic) == 1
    finally:
        release.set()
        await adapter.stop()
