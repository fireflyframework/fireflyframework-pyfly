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
"""Tests for :class:`MessagingAutoConfiguration` — provider routing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pyfly.messaging.auto_configuration import MessagingAutoConfiguration


@pytest.mark.parametrize(
    ("available_modules", "expected_provider"),
    [
        # aiokafka installed → kafka (highest precedence).
        ({"aiokafka"}, "kafka"),
        # aio_pika without aiokafka → rabbitmq.
        ({"aio_pika"}, "rabbitmq"),
        # Neither installed → memory.
        (set(), "memory"),
        # Both installed → kafka wins.
        ({"aiokafka", "aio_pika"}, "kafka"),
    ],
)
def test_detect_provider_parametrized(
    available_modules: set[str],
    expected_provider: str,
) -> None:
    """detect_provider() returns the right messaging provider for each installed-module combination."""
    with patch("pyfly.config.auto.AutoConfiguration.is_available") as is_avail:
        is_avail.side_effect = lambda mod: mod in available_modules
        assert MessagingAutoConfiguration.detect_provider() == expected_provider


def test_the_kafka_adapter_gets_the_listener_settings() -> None:
    from pyfly.core.config import Config
    from pyfly.messaging.adapters.kafka import KafkaAdapter
    from pyfly.messaging.listener_container import ExponentialBackOff

    config = Config(
        {
            "pyfly": {
                "messaging": {
                    "provider": "kafka",
                    "kafka": {
                        "bootstrap-servers": "broker:9092",
                        "auto-offset-reset": "earliest",
                        "dlt": {"suffix": ".dead"},
                    },
                    "listener": {
                        "transactional": "false",
                        "datasource": "reporting",
                        "shutdown-timeout": "3",
                        "retry": {"max-attempts": "7", "initial-delay": "0.5", "multiplier": "3", "max-delay": "9"},
                    },
                }
            }
        }
    )
    adapter = MessagingAutoConfiguration().message_broker(config)
    assert isinstance(adapter, KafkaAdapter)
    settings = adapter._settings
    assert settings.transactional is False
    assert settings.datasource == "reporting"
    assert settings.shutdown_timeout == 3.0
    assert settings.retry.max_attempts == 7
    assert settings.retry.backoff == ExponentialBackOff(initial=0.5, multiplier=3.0, max_delay=9.0)
    assert adapter._auto_offset_reset == "earliest"
    assert adapter._dead_letter_suffix == ".dead"

    config = Config({"pyfly": {"messaging": {"provider": "kafka", "kafka": {"dlt": {"enabled": "false"}}}}})
    adapter = MessagingAutoConfiguration().message_broker(config)
    assert isinstance(adapter, KafkaAdapter)
    assert adapter._dead_letter_suffix is None
    assert adapter._settings.retry.max_attempts == 5  # the defaults: 5 attempts, 1 s doubling up to 30 s
    assert adapter._settings.retry.backoff == ExponentialBackOff()


def test_the_rabbitmq_adapter_gets_the_prefetch_and_dead_letter_exchange() -> None:
    from pyfly.core.config import Config
    from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

    config = Config(
        {
            "pyfly": {
                "messaging": {
                    "provider": "rabbitmq",
                    "rabbitmq": {"url": "amqp://x/", "prefetch": "50", "dead-letter-exchange": "dead"},
                    "listener": {"concurrency": "4"},
                }
            }
        }
    )
    adapter = MessagingAutoConfiguration().message_broker(config)
    assert isinstance(adapter, RabbitMQAdapter)
    assert adapter._settings.prefetch == 50
    assert adapter._settings.concurrency == 4
    assert adapter._dead_letter_exchange == "dead"


def test_a_setting_that_does_not_parse_names_its_key() -> None:
    from pyfly.core.config import Config

    config = Config({"pyfly": {"messaging": {"provider": "kafka", "listener": {"retry": {"max-attempts": "many"}}}}})
    with pytest.raises(ValueError, match="pyfly.messaging.listener.retry.max-attempts"):
        MessagingAutoConfiguration().message_broker(config)
