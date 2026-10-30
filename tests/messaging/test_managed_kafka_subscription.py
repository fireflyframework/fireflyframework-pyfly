from __future__ import annotations

import asyncio

import pytest

from pyfly.messaging.adapters.kafka import KafkaAdapter, KafkaSecurity
from tests.messaging.brokers import FakeKafkaCluster
from tests.messaging.listener_app import eventually


@pytest.mark.asyncio
async def test_stop_member_preserves_group_and_other_subscription():
    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(
        consumer_factory=cluster.consumer, producer_factory=cluster.producer, auto_offset_reset="earliest"
    )
    seen = []

    async def first(message):
        seen.append("first")

    async def second(message):
        seen.append("second")

    await adapter.start()
    one = await adapter.subscribe_managed("orders", first, group="group")
    two = await adapter.subscribe_managed("orders", second, group="group")
    await adapter.publish("orders", b"one")
    await eventually(lambda: seen == ["first", "second"])
    await asyncio.gather(one.stop(), one.stop())
    await adapter.publish("orders", b"two")
    await eventually(lambda: seen == ["first", "second", "second"])
    assert len(adapter._containers) == 1
    await two.stop()
    assert not adapter._containers
    await adapter.stop()


@pytest.mark.asyncio
async def test_failed_registration_rolls_back(monkeypatch):
    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(consumer_factory=cluster.consumer, producer_factory=cluster.producer)
    await adapter.start()

    async def handler(message):
        pass

    original = adapter._start_container

    async def fail(*args):
        raise RuntimeError("failure")

    monkeypatch.setattr(adapter, "_start_container", fail)
    with pytest.raises(RuntimeError):
        await adapter.subscribe_managed("orders", handler, group="group")
    assert not adapter._dispatch
    monkeypatch.setattr(adapter, "_start_container", original)
    handle = await adapter.subscribe_managed("orders", handler, group="group")
    await handle.stop()
    await adapter.stop()


def test_security_is_shared_by_producer_and_consumers_without_repr_secret():
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return object()

    security = KafkaSecurity(
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="PLAIN",
        sasl_plain_username="user",
        sasl_plain_password="secret",
    )
    assert "secret" not in repr(security)
    adapter = KafkaAdapter(security=security, producer_factory=factory, consumer_factory=factory)
    adapter._new_producer()
    adapter._new_consumer(group_id="g")
    assert all(
        row["security_protocol"] == "SASL_PLAINTEXT" and row["sasl_plain_password"] == "secret" for row in created
    )


@pytest.mark.asyncio
async def test_stop_drains_inflight_then_restart_preserves_committed_offset():
    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(
        consumer_factory=cluster.consumer, producer_factory=cluster.producer, auto_offset_reset="earliest"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(message):
        seen.append(message.value)
        entered.set()
        await release.wait()

    await adapter.start()
    handle = await adapter.subscribe_managed("orders", handler, group="stable")
    await adapter.publish("orders", b"one")
    await asyncio.wait_for(entered.wait(), 2)
    stopped = asyncio.create_task(handle.stop())
    await asyncio.sleep(0.05)
    assert not stopped.done()
    release.set()
    await stopped
    again = await adapter.subscribe_managed("orders", handler, group="stable")
    await adapter.publish("orders", b"two")
    await eventually(lambda: seen == [b"one", b"two"])
    await again.stop()
    await adapter.stop()


def test_security_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        KafkaSecurity(security_protocol="SASL_SSL", sasl_mechanism="PLAIN")
    with pytest.raises(ValueError):
        KafkaSecurity(security_protocol="SASL_PLAINTEXT", sasl_mechanism="OAUTHBEARER")


@pytest.mark.asyncio
async def test_cancelled_stop_finishes_draining_and_unregisters(monkeypatch):
    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(consumer_factory=cluster.consumer, producer_factory=cluster.producer)
    await adapter.start()

    async def handler(message):
        pass

    handle = await adapter.subscribe_managed("orders", handler, group="stable")
    container = adapter._containers[("orders", "stable")]
    entered, release = asyncio.Event(), asyncio.Event()
    original = container.stop

    async def delayed():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(container, "stop", delayed)
    task = asyncio.create_task(handle.stop())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not adapter._containers
    assert not adapter._dispatch
    await handle.stop()
    await adapter.stop()


def test_managed_ports_are_public_and_additive():
    from pyfly.messaging import ManagedMessageBrokerPort, ManagedSubscription, MessageBrokerPort

    adapter = KafkaAdapter()
    assert isinstance(adapter, ManagedMessageBrokerPort)
    assert isinstance(adapter, MessageBrokerPort)
    assert hasattr(ManagedSubscription, "stop")


@pytest.mark.asyncio
async def test_unconfirmed_drain_keeps_consumer_and_registration_until_handler_exits():
    from pyfly.messaging.listener_container import ListenerContainerSettings, UnconfirmedDrainError

    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(shutdown_timeout=0.01),
    )
    entered, release, suppressed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(message):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            suppressed.set()
            await release.wait()

    await adapter.start()
    handle = await adapter.subscribe_managed("orders", handler, group="stable")
    await adapter.publish("orders", b"one")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(UnconfirmedDrainError):
            await handle.stop()
        assert suppressed.is_set()
        container = adapter._containers[("orders", "stable")]
        assert not container._task.done()
        assert not cluster.consumers[0].closed
        assert not handle._stopped
        with pytest.raises(UnconfirmedDrainError):
            await adapter.subscribe_managed("orders", handler, group="stable")
        with pytest.raises(UnconfirmedDrainError):
            await handle.stop()
        assert not container._task.done()
    finally:
        release.set()
        await handle.stop()
        await adapter.stop()


@pytest.mark.asyncio
async def test_retain_error_never_dead_letters_or_commits_before_recovery():
    from pyfly.messaging.listener_container import (
        FixedBackOff,
        ListenerContainerSettings,
        RetainMessageError,
        RetryPolicy,
    )

    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(
            retry=RetryPolicy(max_attempts=1, backoff=FixedBackOff(0.01)), poll_timeout=0.01
        ),
    )
    attempts = []
    ready, success = asyncio.Event(), asyncio.Event()

    async def handler(message):
        attempts.append(message.offset)
        if not ready.is_set():
            raise RetainMessageError("retry later")
        success.set()

    await adapter.start()
    handle = await adapter.subscribe_managed("orders", handler, group="stable")
    try:
        await adapter.publish("orders", b"one")
        await eventually(lambda: len(attempts) >= 3)
        assert cluster.committed_offset("stable", "orders") is None
        assert not cluster.records("orders.DLT")
        ready.set()
        await asyncio.wait_for(success.wait(), 2)
    finally:
        await handle.stop()
        await adapter.stop()
    assert set(attempts) == {0}
    assert cluster.committed_offset("stable", "orders") == 1


@pytest.mark.asyncio
async def test_adapter_stop_does_not_close_or_forget_unconfirmed_consumer():
    from pyfly.messaging.listener_container import ListenerContainerSettings, UnconfirmedDrainError

    cluster = FakeKafkaCluster()
    adapter = KafkaAdapter(
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
        auto_offset_reset="earliest",
        settings=ListenerContainerSettings(shutdown_timeout=0.01),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(message):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    await adapter.start()
    await adapter.subscribe_managed("orders", handler, group="stable")
    await adapter.publish("orders", b"one")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(UnconfirmedDrainError):
            await adapter.stop()
        assert ("orders", "stable") in adapter._containers
        assert not cluster.consumers[0].closed
        assert cluster.producers[0].started
    finally:
        release.set()
        await adapter.stop()


def test_retain_backoff_stays_bounded_after_many_attempts():
    from pyfly.messaging.listener_container import ExponentialBackOff, RetainMessageError, RetryPolicy

    policy = RetryPolicy(max_attempts=1, backoff=ExponentialBackOff(initial=1.0, multiplier=1e300))
    assert policy.retry_delay(RetainMessageError(), 1000000) == 30.0
