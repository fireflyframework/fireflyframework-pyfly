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
"""Tests for messaging decorators."""

from __future__ import annotations

from pyfly.messaging.decorators import message_listener
from pyfly.messaging.types import Message


class TestMessageListenerDecorator:
    def test_marks_function_with_metadata(self) -> None:
        """Decorator should set __pyfly_message_listener__, __pyfly_listener_topic__, and __pyfly_listener_group__."""

        @message_listener("orders", group="workers")
        async def handle_order(msg: Message) -> None:
            pass

        assert handle_order.__pyfly_message_listener__ is True
        assert handle_order.__pyfly_listener_topic__ == "orders"
        assert handle_order.__pyfly_listener_group__ == "workers"

    def test_metadata_without_group(self) -> None:
        """When no group is provided, __pyfly_listener_group__ should be None."""

        @message_listener("events")
        async def handle_event(msg: Message) -> None:
            pass

        assert handle_event.__pyfly_message_listener__ is True
        assert handle_event.__pyfly_listener_topic__ == "events"
        assert handle_event.__pyfly_listener_group__ is None

    def test_preserves_function_name(self) -> None:
        """Decorator should preserve __name__ and __doc__."""

        @message_listener("topic")
        async def my_handler(msg: Message) -> None:
            """My handler docstring."""

        assert my_handler.__name__ == "my_handler"
        assert my_handler.__doc__ == "My handler docstring."

    async def test_decorated_function_still_callable(self) -> None:
        """The decorated function should still be callable and execute correctly."""
        received: list[Message] = []

        @message_listener("orders")
        async def handle_order(msg: Message) -> None:
            received.append(msg)

        msg = Message(topic="orders", value=b"test")
        await handle_order(msg)

        assert len(received) == 1
        assert received[0].value == b"test"
