# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`EdaAutoConfiguration` — provider routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.auto_configuration import EdaAutoConfiguration


def _config(values: dict[str, object]) -> object:
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None: values.get(key, default)
    return cfg


class TestEdaAutoConfiguration:
    def test_memory_provider(self) -> None:
        bus = EdaAutoConfiguration().event_publisher(_config({"pyfly.eda.provider": "memory"}))
        assert isinstance(bus, InMemoryEventBus)

    def test_kafka_provider(self) -> None:
        from pyfly.eda.adapters.kafka import KafkaEventBus

        bus = EdaAutoConfiguration().event_publisher(
            _config(
                {
                    "pyfly.eda.provider": "kafka",
                    "pyfly.eda.destinations": "flydesk.idp.jobs",
                    "pyfly.eda.kafka.bootstrap-servers": "kafka:9092",
                }
            )
        )
        assert isinstance(bus, KafkaEventBus)
        assert bus._bootstrap_servers == "kafka:9092"
        assert bus._topics == ["flydesk.idp.jobs"]

    def test_redis_provider(self) -> None:
        with patch("redis.asyncio.Redis.from_url", return_value=MagicMock()):
            from pyfly.eda.adapters.redis import RedisStreamsEventBus

            bus = EdaAutoConfiguration().event_publisher(
                _config(
                    {
                        "pyfly.eda.provider": "redis",
                        "pyfly.eda.redis.url": "redis://r:6379/1",
                        "pyfly.eda.destinations": "a, b",
                        "pyfly.eda.group": "flydesk-idp",
                    }
                )
            )
            assert isinstance(bus, RedisStreamsEventBus)
            assert bus._streams == ["a", "b"]
            assert bus._group == "flydesk-idp"

    def test_postgres_provider(self) -> None:
        from pyfly.eda.adapters.postgres import PostgresEventBus

        bus = EdaAutoConfiguration().event_publisher(
            _config(
                {
                    "pyfly.eda.provider": "postgres",
                    "pyfly.eda.postgres.dsn": "postgresql://x/y",
                    "pyfly.eda.destinations": "flydesk.idp.jobs",
                    "pyfly.eda.postgres.channel": "flydesk_eda",
                }
            )
        )
        assert isinstance(bus, PostgresEventBus)
        assert bus._destinations == ["flydesk.idp.jobs"]
        assert bus._channel == "flydesk_eda"

    def test_postgres_provider_requires_dsn(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="postgres.dsn is required"):
            EdaAutoConfiguration().event_publisher(_config({"pyfly.eda.provider": "postgres"}))

    def test_auto_provider_picks_kafka_when_available(self) -> None:
        with patch("pyfly.config.auto.AutoConfiguration.is_available") as is_avail:
            is_avail.side_effect = lambda mod: mod == "aiokafka"
            assert EdaAutoConfiguration.detect_provider() == "kafka"

    def test_auto_provider_picks_postgres_over_redis(self) -> None:
        with patch("pyfly.config.auto.AutoConfiguration.is_available") as is_avail:
            is_avail.side_effect = lambda mod: mod in ("asyncpg", "redis")
            assert EdaAutoConfiguration.detect_provider() == "postgres"

    def test_auto_provider_falls_back_to_memory(self) -> None:
        with patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=False):
            assert EdaAutoConfiguration.detect_provider() == "memory"


import pytest  # noqa: E402 — local import kept near the tests that use it


@pytest.mark.parametrize(
    ("available_modules", "expected_provider"),
    [
        # All present → kafka wins (highest precedence).
        ({"aiokafka", "asyncpg", "redis", "aio_pika"}, "kafka"),
        # Only asyncpg → postgres.
        ({"asyncpg"}, "postgres"),
        # Only redis → redis.
        ({"redis"}, "redis"),
        # Only aio_pika → rabbitmq.
        ({"aio_pika"}, "rabbitmq"),
        # Nothing → memory.
        (set(), "memory"),
        # Precedence: kafka > postgres > redis > rabbitmq > memory.
        ({"asyncpg", "redis", "aio_pika"}, "postgres"),
        ({"redis", "aio_pika"}, "redis"),
    ],
)
def test_detect_provider_parametrized(
    available_modules: set[str],
    expected_provider: str,
) -> None:
    """detect_provider() returns the right provider for each installed-module combination."""
    with patch("pyfly.config.auto.AutoConfiguration.is_available") as is_avail:
        is_avail.side_effect = lambda mod: mod in available_modules
        assert EdaAutoConfiguration.detect_provider() == expected_provider
