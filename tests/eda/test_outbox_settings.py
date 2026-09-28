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
"""``pyfly.eda.outbox.*``: the outbox bus settings, parsed and validated."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pyfly.core.config import Config
from pyfly.eda.outbox import Outbox, OutboxRelay, OutboxSettings, Retention, StartPosition, subscription_key
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from pyfly.testing import pyfly_config


def _settings(values: dict[str, object]) -> OutboxSettings:
    return OutboxSettings.from_config(pyfly_config(base={f"pyfly.eda.outbox.{k}": v for k, v in values.items()}))


def test_the_defaults() -> None:
    settings = OutboxSettings.from_config(Config({}))
    assert settings == OutboxSettings()
    assert settings.retention == Retention(delivered=timedelta(hours=1), max_age=None)
    assert (settings.create_tables, settings.notify) == (None, None)


def test_durations_numbers_and_switches_parse() -> None:
    settings = _settings(
        {
            "poll-interval": "500ms",
            "claim-timeout": "2m",
            "handler-timeout": "none",
            "batch-size": "20",
            "start": "EARLIEST",
            "error-strategy": "retry",
            "retention.delivered": "0",
            "retention.max-age": "168h",
            "retention.interval": "30s",
            "retention.batch-size": 50,
            "auto-create-tables": "false",
            "notify": "true",
        }
    )
    assert (settings.poll_interval, settings.claim_timeout, settings.handler_timeout) == (0.5, 120.0, None)
    assert settings.batch_size == 20
    assert settings.start_position is StartPosition.EARLIEST
    assert settings.error_strategy is ErrorStrategy.RETRY
    assert settings.retention == Retention(
        delivered=None, max_age=timedelta(hours=168), interval=timedelta(seconds=30), batch_size=50
    )
    assert (settings.create_tables, settings.notify) == (False, True)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("poll-interval", "soon", "pyfly.eda.outbox.poll-interval"),
        ("error-strategy", "panic", "pyfly.eda.outbox.error-strategy must be one of"),
        ("start", "yesterday", "'latest' or 'earliest'"),
        ("batch-size", "many", "pyfly.eda.outbox.batch-size"),
        ("notify", "maybe", "pyfly.eda.outbox.notify"),
    ],
)
def test_a_value_that_does_not_parse_names_its_key(key: str, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _settings({key: value})


def test_the_relay_refuses_a_lease_shorter_than_a_handler() -> None:
    """A delivery claimed again while its handler still runs would be handled twice at once."""
    with pytest.raises(ValueError, match="claim_timeout"):
        OutboxRelay(Outbox(), group="g", claim_timeout=30, handler_timeout=60)


def test_subscription_keys_are_stable_and_distinct() -> None:
    class Worker:
        async def handle(self, envelope: EventEnvelope) -> None:
            del envelope

    async def standalone(envelope: EventEnvelope) -> None:
        del envelope

    assert (
        subscription_key("order.*", Worker().handle)
        == f"order.* {__name__}.test_subscription_keys_are_stable_and_distinct.<locals>.Worker.handle"
    )
    assert subscription_key("order.*", Worker().handle) == subscription_key("order.*", Worker().handle)
    assert subscription_key("*", standalone).startswith(f"* {__name__}.")

    relay = OutboxRelay(Outbox(), group="g")
    first = relay.subscribe("*", standalone)
    second = relay.subscribe("*", standalone)
    assert second.key == f"{first.key}#2"
