from __future__ import annotations

import asyncio

import pytest

from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter
from tests.messaging.brokers import FakeAmqpBroker


@pytest.mark.asyncio
async def test_stop_one_subscription_is_idempotent_and_preserves_other_queue():
    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(connection_factory=broker.connect)

    async def handler(message):
        pass

    await adapter.start()
    one = await adapter.subscribe_managed("orders", handler, group="orders-q")
    two = await adapter.subscribe_managed("audit", handler, group="audit-q")
    await asyncio.gather(one.stop(), one.stop())
    assert len(adapter._handlers) == 1
    assert len(adapter._containers) == 1
    assert "orders-q" in broker.queues
    assert broker.route("pyfly", "audit") == {"audit-q"}
    await two.stop()
    await adapter.stop()


@pytest.mark.asyncio
async def test_failed_registration_rolls_back_and_can_retry(monkeypatch):
    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(connection_factory=broker.connect)
    await adapter.start()

    async def handler(message):
        pass

    original = adapter._start_consumer

    async def fail(*args):
        raise RuntimeError("failure")

    monkeypatch.setattr(adapter, "_start_consumer", fail)
    with pytest.raises(RuntimeError):
        await adapter.subscribe_managed("orders", handler, group="orders-q")
    assert not adapter._handlers
    monkeypatch.setattr(adapter, "_start_consumer", original)
    handle = await adapter.subscribe_managed("orders", handler, group="orders-q")
    assert len(adapter._handlers) == 1
    await handle.stop()
    await adapter.stop()


@pytest.mark.asyncio
async def test_start_failure_closes_partial_consumers_and_can_restart(monkeypatch):
    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(connection_factory=broker.connect)

    async def handler(message):
        pass

    first = await adapter.subscribe_managed("one", handler, group="one-q")
    second = await adapter.subscribe_managed("two", handler, group="two-q")
    original = adapter._start_consumer

    async def fail_second(topic, handler, group):
        if topic == "two":
            raise RuntimeError("failure")
        return await original(topic, handler, group)

    monkeypatch.setattr(adapter, "_start_consumer", fail_second)
    with pytest.raises(RuntimeError):
        await adapter.start()
    assert not adapter._started
    assert not adapter._containers
    assert broker.connections[0].closed
    monkeypatch.setattr(adapter, "_start_consumer", original)
    await adapter.start()
    assert len(adapter._containers) == 2
    await first.stop()
    await second.stop()
    await adapter.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_still_drains_and_does_not_leak_registration(monkeypatch):
    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(connection_factory=broker.connect)
    await adapter.start()

    async def handler(message):
        pass

    handle = await adapter.subscribe_managed("orders", handler, group="orders-q")
    container = handle._container
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
    assert not adapter._handlers
    await handle.stop()
    await adapter.stop()


@pytest.mark.asyncio
async def test_unconfirmed_drain_keeps_channel_and_registration_until_handler_exits():
    from pyfly.messaging.listener_container import ListenerContainerSettings, UnconfirmedDrainError

    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(
        connection_factory=broker.connect, settings=ListenerContainerSettings(shutdown_timeout=0.01)
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
    handle = await adapter.subscribe_managed("orders", handler, group="orders-q")
    broker.publish_to("pyfly", "orders", b"one")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(UnconfirmedDrainError):
            await handle.stop()
        assert suppressed.is_set()
        assert handle._container in adapter._containers
        assert handle._container._channel is not None
        assert not handle._stopped
        with pytest.raises(UnconfirmedDrainError):
            await adapter.subscribe_managed("orders", handler, group="orders-q")
        with pytest.raises(UnconfirmedDrainError):
            await handle.stop()
        assert handle._container._in_flight
    finally:
        release.set()
        await handle.stop()
        await adapter.stop()


@pytest.mark.asyncio
async def test_retain_error_holds_delivery_without_ack_or_dead_letter_until_recovery():
    from pyfly.messaging.listener_container import (
        FixedBackOff,
        ListenerContainerSettings,
        RetainMessageError,
        RetryPolicy,
    )
    from tests.messaging.listener_app import eventually

    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(
        connection_factory=broker.connect,
        settings=ListenerContainerSettings(retry=RetryPolicy(max_attempts=1, backoff=FixedBackOff(0.01))),
    )
    attempts = []
    ready, success = asyncio.Event(), asyncio.Event()

    async def handler(message):
        attempts.append(message.value)
        if not ready.is_set():
            raise RetainMessageError("retry later")
        success.set()

    await adapter.start()
    handle = await adapter.subscribe_managed("orders", handler, group="orders-q")
    try:
        broker.publish_to("pyfly", "orders", b"one")
        await eventually(lambda: len(attempts) >= 3)
        assert not broker.outcomes
        assert not broker.messages("orders-q.dlq")
        ready.set()
        await asyncio.wait_for(success.wait(), 2)
    finally:
        await handle.stop()
        await adapter.stop()
    assert len(broker.outcomes) == 1 and broker.outcomes[0][0] == "ack"


@pytest.mark.asyncio
async def test_adapter_stop_does_not_close_or_forget_unconfirmed_consumer():
    from pyfly.messaging.listener_container import ListenerContainerSettings, UnconfirmedDrainError

    broker = FakeAmqpBroker()
    adapter = RabbitMQAdapter(
        connection_factory=broker.connect, settings=ListenerContainerSettings(shutdown_timeout=0.01)
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(message):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    await adapter.start()
    handle = await adapter.subscribe_managed("orders", handler, group="orders-q")
    broker.publish_to("pyfly", "orders", b"one")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(UnconfirmedDrainError):
            await adapter.stop()
        assert handle._container in adapter._containers
        assert not broker.connections[0].closed
    finally:
        release.set()
        await adapter.stop()
