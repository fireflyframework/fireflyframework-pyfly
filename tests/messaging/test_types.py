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
"""Tests for messaging types and port protocol."""

from __future__ import annotations

from pyfly.messaging.ports.outbound import MessageBrokerPort
from pyfly.messaging.types import Message


class TestMessageBrokerPortProtocol:
    def test_protocol_is_runtime_checkable(self) -> None:
        """MessageBrokerPort should be usable with isinstance() checks."""

        class _FakeBroker:
            async def publish(
                self,
                topic: str,
                value: bytes,
                *,
                key: bytes | None = None,
                headers: dict[str, str] | None = None,
            ) -> None: ...

            async def subscribe(self, topic: str, handler, group: str | None = None) -> None: ...

            async def start(self) -> None: ...

            async def stop(self) -> None: ...

        assert isinstance(_FakeBroker(), MessageBrokerPort)


class TestMessage:
    def test_message_creation(self) -> None:
        """Message should be created with topic and value; key=None and headers={} by default."""
        msg = Message(topic="orders", value=b"hello")
        assert msg.topic == "orders"
        assert msg.value == b"hello"
        assert msg.key is None
        assert msg.headers == {}

    def test_message_with_key_and_headers(self) -> None:
        """Message should accept optional key and headers."""
        msg = Message(
            topic="events",
            value=b"payload",
            key=b"key-1",
            headers={"trace-id": "abc123"},
        )
        assert msg.key == b"key-1"
        assert msg.headers == {"trace-id": "abc123"}
