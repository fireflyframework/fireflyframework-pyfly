# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`EdaAutoConfiguration` — provider routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from pyfly.data.relational.datasource_registry import DataSourceConfigurationError, DataSourceRegistry
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.testing import pyfly_config


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

    def test_postgres_provider(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.postgres import PostgresEventBus

        config = pyfly_config(
            base={
                "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                "pyfly.eda.provider": "postgres",
                "pyfly.eda.postgres.dsn": "postgresql://x/y",
                "pyfly.eda.destinations": "flydesk.idp.jobs",
                "pyfly.eda.postgres.channel": "flydesk_eda",
            }
        )
        bus = EdaAutoConfiguration().event_publisher(config)
        assert isinstance(bus, PostgresEventBus)
        assert bus._destinations == ["flydesk.idp.jobs"]
        assert bus._channel == "flydesk_eda"
        # pyfly.eda.postgres.dsn is an alias resolved through the registry: a datasource of its own, "eda".
        datasource = bus.outbox.datasource
        assert datasource is DataSourceRegistry.for_config(config).get("eda")

    def test_postgres_provider_without_a_dsn_uses_the_primary_datasource(self, tmp_path: Path) -> None:
        """Before 26.09.08 pyfly.eda.postgres.dsn was required: the bus opened a pool of its own."""
        config = pyfly_config(
            base={
                "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                "pyfly.eda.provider": "postgres",
            }
        )
        bus = EdaAutoConfiguration().event_publisher(config)
        assert bus.outbox.datasource is DataSourceRegistry.for_config(config).primary  # type: ignore[attr-defined]

    def test_postgres_provider_without_any_datasource_names_the_missing_url(self) -> None:
        import pytest

        with pytest.raises(DataSourceConfigurationError, match="pyfly.data.relational.url"):
            EdaAutoConfiguration().event_publisher(pyfly_config(base={"pyfly.eda.provider": "postgres"}))

    def test_database_provider_takes_the_outbox_settings(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.database import DatabaseEventBus
        from pyfly.eda.outbox import StartPosition
        from pyfly.eda.types import ErrorStrategy

        config = pyfly_config(
            base={
                "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                "pyfly.data.relational.datasources.reporting.url": f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
                "pyfly.eda.provider": "database",
                "pyfly.eda.group": "billing",
                "pyfly.eda.outbox.datasource": "reporting",
                "pyfly.eda.outbox.poll-interval": "250ms",
                "pyfly.eda.outbox.start": "earliest",
                "pyfly.eda.outbox.error-strategy": "log_and_continue",
                "pyfly.eda.outbox.auto-create-tables": "false",
            }
        )
        bus = EdaAutoConfiguration().event_publisher(config)
        assert type(bus) is DatabaseEventBus
        assert bus.group == "billing"
        assert bus.outbox.datasource is DataSourceRegistry.for_config(config).get("reporting")
        assert bus.relay.poll_interval == 0.25
        assert bus.relay._start_position is StartPosition.EARLIEST
        assert bus.relay._error_strategy is ErrorStrategy.LOG_AND_CONTINUE
        assert bus.outbox.creates_tables is False

    def test_the_outbox_tables_follow_the_schema_strategy_by_default(self, tmp_path: Path) -> None:
        """Unset, the outbox creates its framework tables only where the other framework stores do: when
        ``pyfly.data.relational.ddl-auto`` lets them (``create``, ``create-drop``); otherwise it only checks them."""
        for ddl_auto, creates in (("none", False), ("validate", False), ("create", True), ("create-drop", True)):
            config = pyfly_config(
                base={
                    "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                    "pyfly.data.relational.ddl-auto": ddl_auto,
                    "pyfly.eda.provider": "database",
                }
            )
            bus = EdaAutoConfiguration().event_publisher(config)
            assert bus.outbox.creates_tables is creates, ddl_auto

    def test_an_explicit_auto_create_tables_wins_over_the_schema_strategy(self, tmp_path: Path) -> None:
        url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
        for key, value, ddl_auto in (
            ("pyfly.eda.outbox.auto-create-tables", "true", "none"),
            ("pyfly.eda.outbox.auto-create-tables", "false", "create"),
            ("pyfly.eda.postgres.auto-create-tables", "true", "none"),
        ):
            provider = "postgres" if ".postgres." in key else "database"
            config = pyfly_config(
                base={
                    "pyfly.data.relational.url": url,
                    "pyfly.data.relational.ddl-auto": ddl_auto,
                    "pyfly.eda.provider": provider,
                    key: value,
                }
            )
            bus = EdaAutoConfiguration().event_publisher(config)
            assert bus.outbox.creates_tables is (value == "true"), key

    def test_a_datasource_and_a_url_for_the_bus_are_exclusive(self, tmp_path: Path) -> None:
        import pytest

        config = pyfly_config(
            base={
                "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                "pyfly.eda.provider": "postgres",
                "pyfly.eda.postgres.datasource": "primary",
                "pyfly.eda.postgres.dsn": "postgresql://x/y",
            }
        )
        with pytest.raises(DataSourceConfigurationError, match="not both"):
            EdaAutoConfiguration().event_publisher(config)

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


from pyfly.container import bean, configuration  # noqa: E402 — kept near the test that uses them
from pyfly.eda.domain_events import DomainEventPublisher, active_domain_event_publisher  # noqa: E402

_OWN_PUBLISHER = DomainEventPublisher()


@configuration
class _OwnDomainEventPublisher:
    @bean
    def domain_event_publisher(self) -> DomainEventPublisher:
        return _OWN_PUBLISHER


async def test_an_application_s_own_domain_event_publisher_replaces_the_auto_configured_one() -> None:
    """Both were started, and whichever started last silently collected every domain event."""
    from pyfly.context.application_context import ApplicationContext

    context = ApplicationContext(pyfly_config(base={"pyfly.eda.provider": "memory"}))
    context.register_bean(_OwnDomainEventPublisher)
    context.register_bean(EdaAutoConfiguration)
    await context.start()
    try:
        assert context.get_beans_of_type(DomainEventPublisher) == [_OWN_PUBLISHER]
        assert active_domain_event_publisher() is _OWN_PUBLISHER
    finally:
        await context.stop()
    assert active_domain_event_publisher() is None


# -- pyfly.eda.outbox.enabled: the transactional outbox over any provider (WP09b) ----------------------------------


def _outbox_config(tmp_path: Path, **values: object) -> Any:
    base: dict[str, object] = {"pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"}
    base.update(values)
    return pyfly_config(base=base)


class TestTransactionalOutboxConfiguration:
    def test_the_outbox_layer_is_off_by_default_and_when_disabled(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.kafka import KafkaEventBus

        assert isinstance(
            EdaAutoConfiguration().event_publisher(_outbox_config(tmp_path, **{"pyfly.eda.provider": "kafka"})),
            KafkaEventBus,
        )
        disabled = _outbox_config(tmp_path, **{"pyfly.eda.provider": "memory", "pyfly.eda.outbox.enabled": "false"})
        assert isinstance(EdaAutoConfiguration().event_publisher(disabled), InMemoryEventBus)

    def test_enabled_wraps_the_providers_publisher_in_the_outbox(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.kafka import KafkaEventBus
        from pyfly.eda.outbox import SqlOutboxStore
        from pyfly.eda.outbox_forwarding import TransactionalEventPublisher

        config = _outbox_config(
            tmp_path,
            **{
                "pyfly.eda.provider": "kafka",
                "pyfly.eda.kafka.bootstrap-servers": "kafka:9092",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.poll-interval": "2s",
            },
        )
        publisher = EdaAutoConfiguration().event_publisher(config)

        assert isinstance(publisher, TransactionalEventPublisher)
        assert isinstance(publisher.transport, KafkaEventBus)
        assert publisher.transport._bootstrap_servers == "kafka:9092"
        assert isinstance(publisher.store, SqlOutboxStore)
        assert publisher.store.datasource is DataSourceRegistry.for_config(config).primary
        assert publisher.group == "pyfly.forward:kafka"
        forwarder = publisher.forwarder
        assert forwarder.destinations is None  # every destination
        assert forwarder.poll_interval == 2.0  # pyfly.eda.outbox.* is the default of the forwarder's settings
        assert forwarder._retry.max_attempts == 5  # the listener container's retry policy (pyfly.eda.listener.*)

    def test_the_forward_keys_override_the_outbox_settings(self, tmp_path: Path) -> None:
        from pyfly.eda.outbox import StartPosition
        from pyfly.eda.types import ErrorStrategy
        from pyfly.messaging.listener_container import ExponentialBackOff

        config = _outbox_config(
            tmp_path,
            **{
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.poll-interval": "2s",
                "pyfly.eda.outbox.batch-size": "50",
                "pyfly.eda.listener.retry.max-attempts": "9",
                "pyfly.eda.outbox.forward.poll-interval": "250ms",
                "pyfly.eda.outbox.forward.claim-timeout": "2m",
                "pyfly.eda.outbox.forward.handler-timeout": "15s",
                "pyfly.eda.outbox.forward.start": "earliest",
                "pyfly.eda.outbox.forward.error-strategy": "retry",
                "pyfly.eda.outbox.forward.destinations": "orders, payments",
                "pyfly.eda.outbox.forward.group": "orders-to-kafka",
                "pyfly.eda.outbox.forward.retry.initial-delay": "0.5",
                "pyfly.eda.outbox.forward.retry.max-delay": "60",
                "pyfly.eda.outbox.forward.retention.delivered": "10m",
            },
        )
        forwarder = EdaAutoConfiguration().event_publisher(config).forwarder  # type: ignore[attr-defined]

        assert forwarder.group == "orders-to-kafka"
        assert forwarder.destinations == ["orders", "payments"]
        assert (forwarder.poll_interval, forwarder._batch_size) == (0.25, 50)
        assert forwarder._claim_timeout.total_seconds() == 120.0
        assert forwarder._handler_timeout == 15.0
        assert forwarder._start_position is StartPosition.EARLIEST
        assert forwarder._error_strategy is ErrorStrategy.RETRY
        assert forwarder._retention.delivered.total_seconds() == 600.0
        retry = forwarder._retry
        assert retry.max_attempts == 9  # pyfly.eda.listener.retry.* where the forward keys say nothing
        assert isinstance(retry.backoff, ExponentialBackOff)
        assert (retry.backoff.initial, retry.backoff.multiplier, retry.backoff.max_delay) == (0.5, 2.0, 60.0)

    def test_every_destination_is_forwarded_when_the_list_says_so(self, tmp_path: Path) -> None:
        for value in ("*", " "):
            config = _outbox_config(
                tmp_path,
                **{
                    "pyfly.eda.provider": "memory",
                    "pyfly.eda.outbox.enabled": "true",
                    "pyfly.eda.outbox.forward.destinations": value,
                },
            )
            assert EdaAutoConfiguration().event_publisher(config).forwarder.destinations is None  # type: ignore[attr-defined]

    def test_the_outbox_store_follows_the_schema_strategy_unless_told(self, tmp_path: Path) -> None:
        for ddl_auto, explicit, creates in (
            ("create", None, True),
            ("none", None, False),
            ("none", "true", True),
            ("create", "false", False),
        ):
            values: dict[str, object] = {
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.data.relational.ddl-auto": ddl_auto,
            }
            if explicit is not None:
                values["pyfly.eda.outbox.auto-create-tables"] = explicit
            publisher = EdaAutoConfiguration().event_publisher(_outbox_config(tmp_path, **values))
            assert publisher.store.creates_tables is creates, (ddl_auto, explicit)  # type: ignore[attr-defined]

    def test_the_outbox_datasource_can_be_named(self, tmp_path: Path) -> None:
        config = _outbox_config(
            tmp_path,
            **{
                "pyfly.data.relational.datasources.reporting.url": f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.datasource": "reporting",
            },
        )
        publisher = EdaAutoConfiguration().event_publisher(config)
        assert publisher.store.datasource is DataSourceRegistry.for_config(config).get("reporting")  # type: ignore[attr-defined]

    def test_the_database_and_postgres_providers_are_the_outbox_already(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.database import DatabaseEventBus

        config = _outbox_config(tmp_path, **{"pyfly.eda.provider": "database", "pyfly.eda.outbox.enabled": "true"})
        assert type(EdaAutoConfiguration().event_publisher(config)) is DatabaseEventBus

    def test_the_mongo_store_is_not_available_until_it_lands(self, tmp_path: Path) -> None:
        import pytest

        for provider in ("memory", "database", "postgres"):
            config = _outbox_config(
                tmp_path,
                **{
                    "pyfly.eda.provider": provider,
                    "pyfly.eda.outbox.enabled": "true",
                    "pyfly.eda.outbox.store": "mongo",
                },
            )
            with pytest.raises(ValueError, match="not available until the Mongo outbox store lands"):
                EdaAutoConfiguration().event_publisher(config)

    def test_auto_picks_the_store_of_the_applications_datasource(self, tmp_path: Path) -> None:
        import pytest

        from pyfly.eda.outbox import SqlOutboxStore

        document_only = pyfly_config(
            base={
                "pyfly.data.document.enabled": "true",
                "pyfly.data.document.uri": "mongodb://localhost:27017",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
            }
        )
        with pytest.raises(ValueError, match="not available until the Mongo outbox store lands"):
            EdaAutoConfiguration().event_publisher(document_only)

        both = _outbox_config(
            tmp_path,
            **{
                "pyfly.data.document.enabled": "true",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.store": "AUTO",
            },
        )
        assert isinstance(EdaAutoConfiguration().event_publisher(both).store, SqlOutboxStore)  # type: ignore[attr-defined]

    def test_values_that_do_not_parse_name_their_key(self, tmp_path: Path) -> None:
        import pytest

        for key, value, message in (
            ("pyfly.eda.outbox.enabled", "maybe", "pyfly.eda.outbox.enabled"),
            ("pyfly.eda.outbox.store", "redis", "pyfly.eda.outbox.store must be sql, mongo or auto"),
            ("pyfly.eda.outbox.forward.retry.max-attempts", "0", "pyfly.eda.outbox.forward.retry.max-attempts"),
            ("pyfly.eda.outbox.forward.poll-interval", "soon", "pyfly.eda.outbox.forward.poll-interval"),
        ):
            config = _outbox_config(
                tmp_path, **{"pyfly.eda.provider": "memory", "pyfly.eda.outbox.enabled": "true", key: value}
            )
            with pytest.raises(ValueError, match=message):
                EdaAutoConfiguration().event_publisher(config)

    def test_command_and_domain_events_publish_in_the_unit_through_it(self, tmp_path: Path) -> None:
        from pyfly.cqrs.event.publisher import EdaCommandEventPublisher

        config = _outbox_config(tmp_path, **{"pyfly.eda.provider": "memory", "pyfly.eda.outbox.enabled": "true"})
        publisher = EdaAutoConfiguration().event_publisher(config)
        assert getattr(publisher, "joins_transactions", False) is True
        assert EdaCommandEventPublisher(publisher).joins_transactions is True
