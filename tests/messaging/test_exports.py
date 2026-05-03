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
"""Tests for messaging module public exports."""

from __future__ import annotations


class TestMessagingExports:
    def test_can_import_port(self) -> None:
        from pyfly.messaging import MessageBrokerPort

        assert MessageBrokerPort is not None

    def test_can_import_message(self) -> None:
        from pyfly.messaging import Message

        assert Message is not None

    def test_can_import_memory_broker(self) -> None:
        from pyfly.messaging.adapters.memory import InMemoryMessageBroker

        assert InMemoryMessageBroker is not None

    def test_can_import_message_listener(self) -> None:
        from pyfly.messaging import message_listener

        assert message_listener is not None
