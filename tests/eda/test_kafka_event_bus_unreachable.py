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
"""A Kafka bus that cannot reach its broker closes the producer each failed start opened (WP09b).

The outbox forwarder attempts a publish again and again while a broker is down, and every attempt of a bus that
never started opens a producer to start it. One whose ``start()`` failed was left open ("Unclosed
AIOKafkaProducer", its client and sockets with it), once per attempt. Real aiokafka producers, on a local port
nothing listens on.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

pytest.importorskip("aiokafka")

from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]  # noqa: E402
from aiokafka.errors import KafkaConnectionError  # type: ignore[import-untyped]  # noqa: E402

from pyfly.eda.adapters.kafka import KafkaEventBus  # noqa: E402


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_publish_that_cannot_reach_the_broker_leaves_no_producer_open() -> None:
    producers: list[Any] = []

    def producer_factory(**settings: Any) -> Any:
        producer = AIOKafkaProducer(**settings)
        producers.append(producer)
        return producer

    bus = KafkaEventBus(bootstrap_servers=f"127.0.0.1:{_closed_port()}", producer_factory=producer_factory)
    for _attempt in range(3):
        with pytest.raises(KafkaConnectionError):
            await bus.publish("orders", "order.placed", {"n": 1})

    assert len(producers) == 3
    assert [producer._closed for producer in producers] == [True, True, True]
