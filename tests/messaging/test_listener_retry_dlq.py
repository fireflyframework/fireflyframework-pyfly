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
"""@message_listener retry + dead-letter routing (v26.06.47).

On the in-memory broker the handler is retried in process and dead-lettered by republishing. On a broker
with a listener container (Kafka, RabbitMQ), ``wrap_listener`` only hands the listener's settings to the
container, which retries and dead-letters each delivery outside its unit of work (26.09.08).
"""

from __future__ import annotations

import pytest

from pyfly.messaging.error_handling import wrap_listener
from pyfly.messaging.listener_container import LinearBackOff, ListenerEndpoint, ListenerOptions, listener_options
from pyfly.messaging.types import Message


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, bytes | None, dict[str, str] | None]] = []

    async def publish(
        self, topic: str, value: bytes, *, key: bytes | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.published.append((topic, value, key, headers))


def _msg() -> Message:
    return Message(topic="orders", value=b"data", key=b"k", headers={"h": "1"})


@pytest.mark.asyncio
async def test_no_config_returns_handler_unchanged() -> None:
    async def handler(_m: Message) -> None: ...

    assert wrap_listener(handler, _FakeBroker()) is handler  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    async def handler(_m: Message) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")

    broker = _FakeBroker()
    wrapped = wrap_listener(handler, broker, retries=3)  # type: ignore[arg-type]
    await wrapped(_msg())
    assert calls["n"] == 3
    assert broker.published == []  # succeeded before DLQ


@pytest.mark.asyncio
async def test_exhausted_retries_routes_to_dlq() -> None:
    async def handler(_m: Message) -> None:
        raise ValueError("boom")

    broker = _FakeBroker()
    wrapped = wrap_listener(handler, broker, retries=2, dead_letter_topic="orders.DLT")  # type: ignore[arg-type]
    await wrapped(_msg())  # must not raise

    assert len(broker.published) == 1
    topic, value, key, headers = broker.published[0]
    assert topic == "orders.DLT"
    assert value == b"data"
    assert key == b"k"
    assert headers is not None
    assert headers["x-original-topic"] == "orders"
    assert headers["x-exception"] == "ValueError"


@pytest.mark.asyncio
async def test_exhausted_retries_without_dlq_reraises() -> None:
    async def handler(_m: Message) -> None:
        raise ValueError("boom")

    wrapped = wrap_listener(handler, _FakeBroker(), retries=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="boom"):
        await wrapped(_msg())


@pytest.mark.asyncio
async def test_none_settings_keep_the_in_process_defaults() -> None:
    async def handler(_m: Message) -> None: ...

    assert wrap_listener(handler, _FakeBroker(), retries=None, retry_delay=None) is handler  # type: ignore[arg-type]


class _ContainerBroker(_FakeBroker):
    manages_listener_errors = True


@pytest.mark.asyncio
async def test_a_container_broker_gets_the_settings_not_an_in_process_retry() -> None:
    calls = {"n": 0}

    async def handler(_m: Message) -> None:
        calls["n"] += 1
        raise ValueError("boom")

    wrapped = wrap_listener(
        handler,
        _ContainerBroker(),  # type: ignore[arg-type]
        retries=2,
        retry_delay=0.5,
        dead_letter_topic="orders.DLT",
    )
    assert isinstance(wrapped, ListenerEndpoint)
    assert listener_options(wrapped) == ListenerOptions(
        max_attempts=3, backoff=LinearBackOff(0.5), dead_letter="orders.DLT"
    )
    with pytest.raises(ValueError, match="boom"):
        await wrapped(_msg())  # the container retries: the wrapper calls the handler once
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_container_broker_keeps_its_own_policy_for_unset_settings() -> None:
    async def handler(_m: Message) -> None: ...

    wrapped = wrap_listener(handler, _ContainerBroker(), retries=None, retry_delay=None)  # type: ignore[arg-type]
    assert listener_options(wrapped) == ListenerOptions()
