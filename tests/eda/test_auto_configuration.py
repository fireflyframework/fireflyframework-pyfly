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

    def test_the_mongo_store_runs_on_the_document_datasource(self) -> None:
        from pyfly.eda.adapters.database import DatabaseEventBus
        from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
        from pyfly.eda.outbox_forwarding import TransactionalEventPublisher

        base = {
            "pyfly.data.document.enabled": "true",
            "pyfly.data.document.database": "shop",
            "pyfly.data.document.datasource": "catalog",
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.store": "mongo",
        }
        publisher = EdaAutoConfiguration().event_publisher(pyfly_config(base={**base, "pyfly.eda.provider": "memory"}))
        assert isinstance(publisher, TransactionalEventPublisher)
        assert isinstance(publisher.store, MongoOutboxStore)
        # The document datasource's units, resolved when the store runs: an append joins them.
        assert publisher.store.datasource == "catalog"
        assert publisher.store.database == "shop"
        assert publisher.store.creates_indexes is True  # whatever ddl-auto says: an index is not a table

        bus = EdaAutoConfiguration().event_publisher(
            pyfly_config(
                base={**base, "pyfly.eda.provider": "database", "pyfly.eda.outbox.auto-create-tables": "false"}
            )
        )
        assert type(bus) is DatabaseEventBus
        assert isinstance(bus.outbox, MongoOutboxStore)
        assert bus.sql_store is None
        assert bus.outbox.creates_indexes is False

    def test_the_postgres_provider_is_the_sql_outbox(self, tmp_path: Path) -> None:
        import pytest

        config = _outbox_config(
            tmp_path,
            **{
                "pyfly.data.document.enabled": "true",
                "pyfly.eda.provider": "postgres",
                "pyfly.eda.outbox.store": "mongo",
            },
        )
        with pytest.raises(ValueError, match="pyfly.eda.provider=postgres keeps the outbox in PostgreSQL"):
            EdaAutoConfiguration().event_publisher(config)

    def test_the_mongo_store_needs_the_document_data_layer(self, tmp_path: Path) -> None:
        import pytest

        for provider in ("memory", "database"):
            config = _outbox_config(
                tmp_path,
                **{
                    "pyfly.eda.provider": provider,
                    "pyfly.eda.outbox.enabled": "true",
                    "pyfly.eda.outbox.store": "mongo",
                },
            )
            with pytest.raises(ValueError, match="pyfly.data.document.enabled=true"):
                EdaAutoConfiguration().event_publisher(config)

    def test_the_mongo_store_without_the_mongodb_driver_names_the_extra(self) -> None:
        import sys

        import pytest

        config = pyfly_config(
            base={
                "pyfly.data.document.enabled": "true",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.store": "mongo",
            }
        )
        with (
            patch.dict(sys.modules, {"pyfly.eda.adapters.mongo_outbox": None}),  # pymongo is not installed
            pytest.raises(ValueError, match=r"pyfly\[data-document\]") as raised,
        ):
            EdaAutoConfiguration().event_publisher(config)
        assert "enable the document data layer" not in str(raised.value)  # it is enabled already

    def test_auto_picks_the_store_of_the_applications_datasource(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
        from pyfly.eda.outbox import SqlOutboxStore

        document_only = pyfly_config(
            base={
                "pyfly.data.document.enabled": "true",
                "pyfly.data.document.uri": "mongodb://localhost:27017",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
            }
        )
        store = EdaAutoConfiguration().event_publisher(document_only).store  # type: ignore[attr-defined]
        assert isinstance(store, MongoOutboxStore)
        assert store.datasource == "document"

        both = _outbox_config(
            tmp_path,
            **{
                "pyfly.data.document.enabled": "true",
                "pyfly.data.relational.enabled": "true",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.store": "AUTO",
            },
        )
        assert isinstance(EdaAutoConfiguration().event_publisher(both).store, SqlOutboxStore)  # type: ignore[attr-defined]

    def test_auto_keeps_the_relational_layers_sql_store_however_its_url_is_configured(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """With the relational data layer on beside the document one, the store is the SQL one on the registry's
        primary, however its URL is configured: the legacy ``pyfly.data.url`` alias, the ``PYFLY_DATA_RELATIONAL_URL``
        environment variable; an ``outbox.datasource`` names another datasource of the registry."""
        import pytest

        from pyfly.eda.adapters.database import DatabaseEventBus

        document = {
            "pyfly.data.document.enabled": "true",
            "pyfly.data.relational.enabled": "true",
            "pyfly.eda.provider": "database",
        }
        legacy = pyfly_config(base={**document, "pyfly.data.url": f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"})
        bus = EdaAutoConfiguration().event_publisher(legacy)
        assert type(bus) is DatabaseEventBus
        assert bus.sql_store is not None
        assert bus.sql_store.datasource is DataSourceRegistry.for_config(legacy).primary

        named = pyfly_config(
            base={
                **document,
                "pyfly.data.relational.datasources.main.url": f"sqlite+aiosqlite:///{tmp_path / 'main.db'}",
                "pyfly.eda.outbox.datasource": "main",
            }
        )
        assert type(EdaAutoConfiguration().event_publisher(named)) is DatabaseEventBus
        # A named datasource and no outbox.datasource: the SQL store's own error (no primary), not the Mongo one.
        unnamed = pyfly_config(
            base={**document, "pyfly.data.relational.datasources.main.url": f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"}
        )
        with pytest.raises(DataSourceConfigurationError, match="No primary datasource"):
            EdaAutoConfiguration().event_publisher(unnamed)

        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_URL", f"sqlite+aiosqlite:///{tmp_path / 'env.db'}")
        assert type(EdaAutoConfiguration().event_publisher(pyfly_config(base=document))) is DatabaseEventBus

    def test_a_document_uri_without_the_document_layer_is_no_mongo_client(self, tmp_path: Path) -> None:
        """``pyfly.data.document.uri`` alone wires no Mongo client (``pyfly.data.document.enabled`` does): the SQL
        store's error is the one raised, not the Mongo store's."""
        import pytest

        config = pyfly_config(
            base={
                "pyfly.data.document.uri": "mongodb://localhost:27017",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
            }
        )
        with pytest.raises(DataSourceConfigurationError, match="No primary datasource"):
            EdaAutoConfiguration().event_publisher(config)


def _raised_in(error: BaseException, filename: str, function: str) -> bool:
    """Whether *error*, or an exception it was raised from, passed through *function* of *filename*."""
    import traceback

    current: BaseException | None = error
    for _ in range(20):
        if current is None:
            return False
        if any(
            frame.filename.endswith(filename) and frame.name == function
            for frame in traceback.extract_tb(current.__traceback__)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _cause_chain(error: BaseException) -> list[str]:
    chain: list[str] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 20:
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return chain


class TestAutoOutboxStoreInAnApplicationContext:
    """``pyfly.eda.outbox.store=auto`` in a context that has the datasource auto-configuration, whose
    ``DataSourceRegistry`` bean exists whenever SQLAlchemy is installed, a document-only application included."""

    @staticmethod
    async def _start(values: dict[str, object]) -> Any:
        from pyfly.context.application_context import ApplicationContext
        from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration

        context = ApplicationContext(pyfly_config(base=values))
        context.register_bean(DataSourceAutoConfiguration)
        context.register_bean(EdaAutoConfiguration)
        await context.start()
        return context

    async def test_a_document_only_application_gets_the_mongo_store(self) -> None:
        """The document layer's server cannot be reached here: the context fails to start in the Mongo outbox
        store's start, the store auto chose (the SQL store would fail on its missing primary datasource first). The
        replica-set lane runs such a context for real (``tests/integration/test_mongo_outbox_application.py``)."""
        import pytest

        from pyfly.context.application_context import ApplicationContext
        from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration

        for provider in ("memory", "database"):
            context = ApplicationContext(
                pyfly_config(
                    base={
                        "pyfly.data.document.enabled": "true",
                        "pyfly.data.document.uri": "mongodb://localhost:1",
                        "pyfly.data.document.server-selection-timeout": "0.2",
                        "pyfly.eda.provider": provider,
                        "pyfly.eda.outbox.enabled": "true",
                    }
                )
            )
            context.register_bean(DataSourceAutoConfiguration)
            context.register_bean(EdaAutoConfiguration)
            try:
                with pytest.raises(Exception) as raised:
                    await context.start()
            finally:
                await context.stop()  # the lifecycle beans that started before the failure (the domain events')
            chain = _cause_chain(raised.value)
            assert not any("No primary datasource" in link for link in chain), (provider, chain)
            assert _raised_in(raised.value, "mongo_outbox.py", "start"), (provider, chain)

    async def test_an_application_with_a_relational_datasource_keeps_the_sql_store(self, tmp_path: Path) -> None:
        from pyfly.eda.outbox import SqlOutboxStore
        from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
        from pyfly.eda.ports.outbound import EventPublisher

        context = await self._start(
            {
                "pyfly.data.document.enabled": "true",
                "pyfly.data.document.uri": "mongodb://localhost:1",
                "pyfly.data.relational.enabled": "true",
                "pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                "pyfly.eda.provider": "memory",
                "pyfly.eda.outbox.enabled": "true",
                "pyfly.eda.outbox.auto-create-tables": "true",
            }
        )
        try:
            publisher = context.get_bean(EventPublisher)
            assert isinstance(publisher, TransactionalEventPublisher)
            assert isinstance(publisher.store, SqlOutboxStore)
            assert publisher.store.datasource is context.get_bean(DataSourceRegistry).primary
            assert publisher.running
        finally:
            await context.stop()

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


# -- pyfly.eda.outbox.store=auto: one rule, the default @transactional datasource's -------------------------------


async def _default_transaction_datasource(config: Any) -> str:
    """The datasource a plain ``@transactional`` runs on in an application of *config* with the document layer on:
    what the document auto-configuration's registration makes the default of the context's managers."""
    from pyfly.data.document.auto_configuration import DocumentAutoConfiguration
    from pyfly.data.transaction.registry import TransactionManagerRegistry

    registry = TransactionManagerRegistry()
    manager = MagicMock(datasource="document")
    registration = DocumentAutoConfiguration().mongo_transaction_manager_registration(
        config, manager, MagicMock(get=lambda: registry)
    )
    await registration.start()
    try:
        return registry.default_name
    finally:
        await registration.stop()


class TestAutoOutboxStoreRule:
    """``auto`` picks the Mongo store exactly when the document datasource is the default of ``@transactional``, from
    the configuration alone: the outbox and a plain ``@transactional`` never land on two databases, whatever the
    ``DataSourceRegistry`` holds and whichever bean was built first."""

    @staticmethod
    def _store(values: dict[str, object], provider: str = "memory") -> Any:
        config = pyfly_config(base={**values, "pyfly.eda.provider": provider, "pyfly.eda.outbox.enabled": "true"})
        publisher = EdaAutoConfiguration().event_publisher(config)
        return publisher.outbox if provider == "database" else publisher.store  # type: ignore[attr-defined]

    async def test_auto_agrees_with_the_default_transaction_manager(self, tmp_path: Path) -> None:
        from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
        from pyfly.eda.outbox import SqlOutboxStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
        document = {"pyfly.data.document.enabled": "true"}
        cases: list[tuple[str, dict[str, object], str]] = [
            ("document-only", {}, "mongo"),
            ("document-only, dev profile", {"pyfly.profiles.active": "dev"}, "mongo"),
            ("relational URL without the relational layer", {"pyfly.data.relational.url": url}, "mongo"),
            ("legacy URL without the relational layer", {"pyfly.data.url": url}, "mongo"),
            ("polyglot", {"pyfly.data.relational.enabled": "true", "pyfly.data.relational.url": url}, "sql"),
            (
                "polyglot, the document datasource made the default",
                {
                    "pyfly.data.relational.enabled": "true",
                    "pyfly.data.relational.url": url,
                    "pyfly.data.document.transaction.default": "true",
                },
                "mongo",
            ),
            (
                "document-only, the document datasource not the default",
                {"pyfly.data.relational.url": url, "pyfly.data.document.transaction.default": "false"},
                "sql",
            ),
        ]
        for label, values, expected in cases:
            config = {**document, **values}
            default = await _default_transaction_datasource(pyfly_config(base=config))
            assert default == ("document" if expected == "mongo" else "primary"), label
            for provider in ("memory", "database"):
                store = self._store(config, provider)
                kind = MongoOutboxStore if expected == "mongo" else SqlOutboxStore
                assert isinstance(store, kind), (label, provider, type(store).__name__)

    def test_relational_only_applications_get_the_sql_store(self, tmp_path: Path) -> None:
        from pyfly.eda.outbox import SqlOutboxStore

        url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
        for values in (
            {"pyfly.data.relational.enabled": "true", "pyfly.data.relational.url": url},
            {"pyfly.data.relational.url": url},
        ):
            assert isinstance(self._store(values), SqlOutboxStore), values

    def test_another_stores_datasource_does_not_flip_auto(self, tmp_path: Path) -> None:
        """A datasource another framework store registered in the registry (the PostgreSQL cache's
        ``pyfly.cache.postgres.url`` alias, here) is no relational data layer: auto answers the same before and after
        it is registered, so the order the beans are built in does not matter."""
        from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore

        cache_url = f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"
        values: dict[str, object] = {"pyfly.data.document.enabled": "true", "pyfly.cache.postgres.url": cache_url}
        config = pyfly_config(base={**values, "pyfly.eda.provider": "memory", "pyfly.eda.outbox.enabled": "true"})
        before = EdaAutoConfiguration().event_publisher(config).store  # type: ignore[attr-defined]
        DataSourceRegistry.for_config(config).resolve(cache_url, name="cache", url_key="pyfly.cache.postgres.url")
        assert DataSourceRegistry.for_config(config).names()
        after = EdaAutoConfiguration().event_publisher(config).store  # type: ignore[attr-defined]
        assert isinstance(before, MongoOutboxStore)
        assert isinstance(after, MongoOutboxStore)

    def test_an_outbox_datasource_keeps_auto_on_the_sql_store(self, tmp_path: Path) -> None:
        from pyfly.eda.outbox import SqlOutboxStore

        values: dict[str, object] = {
            "pyfly.data.document.enabled": "true",
            "pyfly.data.relational.datasources.main.url": f"sqlite+aiosqlite:///{tmp_path / 'main.db'}",
            "pyfly.eda.outbox.datasource": "main",
        }
        assert isinstance(self._store(values), SqlOutboxStore)
        url_values: dict[str, object] = {
            "pyfly.data.document.enabled": "true",
            "pyfly.eda.outbox.url": f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}",
        }
        assert isinstance(self._store(url_values), SqlOutboxStore)

    def test_the_mongo_store_refuses_an_outbox_datasource(self, tmp_path: Path) -> None:
        import pytest

        for key, value in (
            ("pyfly.eda.outbox.datasource", "main"),
            ("pyfly.eda.outbox.url", f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}"),
        ):
            for provider in ("memory", "database"):
                values: dict[str, object] = {
                    "pyfly.data.document.enabled": "true",
                    "pyfly.eda.outbox.store": "mongo",
                    key: value,
                }
                with pytest.raises(ValueError, match=rf"pyfly\.eda\.outbox\.store=mongo.*{key.replace('.', r'\.')}"):
                    self._store(values, provider)

    def test_the_chosen_store_is_logged_once(self, caplog: Any, tmp_path: Path) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="pyfly.eda.auto_configuration")
        for values, provider, store in (
            ({"pyfly.data.document.enabled": "true"}, "memory", "mongo"),
            ({"pyfly.data.document.enabled": "true"}, "database", "mongo"),
            ({"pyfly.data.relational.url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"}, "database", "sql"),
        ):
            caplog.clear()
            self._store(values, provider)
            records = [r for r in caplog.records if r.getMessage() == "eda_outbox_store"]
            assert len(records) == 1, (values, provider)
            assert records[0].levelno == logging.INFO
            assert records[0].store == store  # type: ignore[attr-defined]
            assert records[0].setting == "auto"  # type: ignore[attr-defined]
            assert records[0].reason  # type: ignore[attr-defined]
