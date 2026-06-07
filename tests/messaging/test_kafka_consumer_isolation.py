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
"""Kafka per-message error isolation (v26.06.35, from the final parity audit):
a single handler exception must not kill the consumer loop (which would silently
stop processing every subsequent message)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pyfly.messaging.adapters.kafka import KafkaAdapter
from pyfly.messaging.types import Message


class _FakeRecord:
    def __init__(self, value: bytes, topic: str = "t", key: bytes = b"k") -> None:
        self.value = value
        self.topic = topic
        self.key = key
        self.headers: list[Any] = []


class _FakeConsumer:
    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for record in self._records:
                yield record

        return _gen()


@pytest.mark.asyncio
async def test_consume_loop_isolates_handler_failure() -> None:
    processed: list[bytes] = []

    async def handler(msg: Message) -> None:
        if msg.value == b"bad":
            raise ValueError("boom")
        processed.append(msg.value)

    adapter = KafkaAdapter("localhost:9092")
    records = [_FakeRecord(b"a"), _FakeRecord(b"bad"), _FakeRecord(b"c")]
    await adapter._consume_loop(_FakeConsumer(records), [handler])
    # The bad message is isolated; the consumer kept processing (a and c both handled).
    assert processed == [b"a", b"c"]


@pytest.mark.asyncio
async def test_consume_loop_propagates_cancellation() -> None:
    async def handler(msg: Message) -> None:
        raise asyncio.CancelledError

    adapter = KafkaAdapter("localhost:9092")
    # CancelledError is re-raised past the per-message guard and stops the loop cleanly
    # via the outer handler — not swallowed as a normal handler failure.
    await adapter._consume_loop(_FakeConsumer([_FakeRecord(b"a")]), [handler])
