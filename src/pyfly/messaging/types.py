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
"""Messaging data types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Message:
    """One message, as a handler receives it.

    Delivery is at-least-once: a message whose handler failed or was interrupted is delivered again, so
    a handler that must not apply a message twice keys its work on where the message came from:
    *partition* and *offset* on Kafka, *message_id* on RabbitMQ (every message the adapter publishes gets
    one, and a redelivery keeps it). *delivery_attempt* counts the deliveries of this message, the first
    being 1.
    """

    topic: str
    value: bytes
    key: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    partition: int | None = None
    offset: int | None = None
    message_id: str | None = None
    delivery_attempt: int = 1
