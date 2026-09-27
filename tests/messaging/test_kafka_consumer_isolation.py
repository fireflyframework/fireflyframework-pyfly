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
"""Kafka per-message error isolation (v26.06.35), with the offsets in view.

One failing record must not kill the consumer (which would silently stop processing every later
message), and since 26.09.08 it must not be skipped either: until then the loop logged the failure and
moved on while auto-commit committed past it, and these tests used an offset-less fake that could not
see the loss (C012, C020). The fake cluster here keeps positions and committed offsets.
"""

from __future__ import annotations

import asyncio

from pyfly.messaging.adapters.kafka import message_of
from pyfly.messaging.listener_container import (
    FixedBackOff,
    KafkaListenerContainer,
    ListenerContainerSettings,
    RetryPolicy,
)
from pyfly.messaging.types import Message
from tests.messaging.brokers import FakeKafkaCluster, FakeRecord
from tests.messaging.listener_app import eventually

SETTINGS = ListenerContainerSettings(
    retry=RetryPolicy(max_attempts=2, backoff=FixedBackOff(0.0)), transactional=False, poll_timeout=0.05
)


def _container(
    cluster: FakeKafkaCluster,
    handler: object,
    dead: list[tuple[bytes, str, int]],
    settings: ListenerContainerSettings = SETTINGS,
) -> KafkaListenerContainer[Message]:
    async def dead_letter(record: FakeRecord, error: BaseException, attempts: int) -> None:
        dead.append((record.value, type(error).__name__, attempts))

    return KafkaListenerContainer(
        topics=["t"],
        group="g",
        consumer_factory=cluster.consumer,
        convert=message_of,
        handler=handler,  # type: ignore[arg-type]
        dead_letter=dead_letter,
        settings=settings,
        auto_offset_reset="earliest",
    )


async def test_a_failing_record_neither_stops_the_consumer_nor_is_skipped() -> None:
    cluster = FakeKafkaCluster()
    for value in (b"a", b"bad", b"c"):
        cluster.append("t", value)
    processed: list[bytes] = []
    attempts: list[bytes] = []
    dead: list[tuple[bytes, str, int]] = []

    async def handler(msg: Message) -> None:
        attempts.append(msg.value)
        if msg.value == b"bad":
            raise ValueError("boom")
        processed.append(msg.value)

    container = _container(cluster, handler, dead)
    await container.start()
    try:
        await eventually(lambda: cluster.committed_offset("g", "t") == 3, what="every record committed")
    finally:
        await container.stop()
    # The bad record was attempted twice, dead-lettered, and only then passed; the consumer kept going.
    assert attempts == [b"a", b"bad", b"bad", b"c"]
    assert processed == [b"a", b"c"]
    assert dead == [(b"bad", "ValueError", 2)]


async def test_cancelling_the_consumer_mid_handler_commits_nothing_in_flight() -> None:
    cluster = FakeKafkaCluster()
    cluster.append("t", b"a")
    started = asyncio.Event()
    dead: list[tuple[bytes, str, int]] = []

    async def handler(msg: Message) -> None:
        started.set()
        await asyncio.Event().wait()  # never finishes on its own

    settings = ListenerContainerSettings(
        retry=SETTINGS.retry, transactional=False, poll_timeout=0.05, shutdown_timeout=0.05
    )
    container = _container(cluster, handler, dead, settings)
    await container.start()
    await started.wait()
    await container.stop()
    assert cluster.committed_offset("g", "t") is None
    assert dead == []
    assert not container.running
