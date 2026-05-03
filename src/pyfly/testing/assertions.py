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
"""Test assertion helpers for event-driven testing."""

from __future__ import annotations

from typing import Any

from pyfly.eda.types import EventEnvelope


def assert_event_published(
    events: list[EventEnvelope],
    event_type: str,
    payload_contains: dict[str, Any] | None = None,
) -> EventEnvelope:
    """Assert that an event of the given type was published.

    Args:
        events: List of captured EventEnvelope instances.
        event_type: Expected event type string.
        payload_contains: If provided, assert that the event payload
            contains these key-value pairs.

    Returns:
        The matching EventEnvelope.

    Raises:
        AssertionError: If no matching event is found.
    """
    matching = [e for e in events if e.event_type == event_type]
    if not matching:
        published_types = [e.event_type for e in events]
        raise AssertionError(f"Expected event '{event_type}' to be published. Published events: {published_types}")

    event = matching[0]
    if payload_contains:
        for key, value in payload_contains.items():
            assert key in event.payload, f"Expected key '{key}' in event payload"
            assert event.payload[key] == value, f"Expected payload['{key}'] == {value!r}, got {event.payload[key]!r}"

    return event


def assert_no_events_published(events: list[EventEnvelope]) -> None:
    """Assert that no events were published.

    Args:
        events: List of captured EventEnvelope instances.

    Raises:
        AssertionError: If any events were published.
    """
    if events:
        types = [e.event_type for e in events]
        raise AssertionError(f"Expected no events to be published. Got: {types}")
