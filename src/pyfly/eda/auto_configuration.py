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
"""EDA subsystem auto-configuration.

Registers an :class:`EventPublisher` bean keyed on ``pyfly.eda.provider``:

* ``memory`` — :class:`InMemoryEventBus` (default if the property is set
  to ``auto`` and no broker library is installed).
* ``kafka`` — :class:`KafkaEventBus`, when ``aiokafka`` is available.
* ``redis`` — :class:`RedisStreamsEventBus`, when ``redis`` is available.
* ``postgres`` — :class:`PostgresEventBus`, when ``asyncpg`` is available: the transactional outbox on a
  datasource of the application's ``DataSourceRegistry``, with LISTEN/NOTIFY wake-ups.
* ``database`` — :class:`DatabaseEventBus`: the same transactional outbox on any SQL datasource (SQLite,
  MySQL, MariaDB, PostgreSQL...), or on MongoDB (``outbox.store``).
* ``rabbitmq`` — :class:`RabbitMqEventBus`, when ``aio_pika`` is available.

Configuration keys (all optional, prefix ``pyfly.eda.``):

* ``provider`` — ``memory | kafka | redis | postgres | database | rabbitmq | auto``.
* ``destinations`` — comma-separated list of topics / streams /
  destinations to consume from. Defaults to ``pyfly.events``.
* ``group`` — consumer group / cursor name. Defaults to
  ``pyfly-default``.
* ``kafka.bootstrap-servers`` — Kafka bootstrap. Default ``localhost:9092``.
* ``kafka.partition-key-header`` — envelope header consulted first for the
  record key (then ``x-correlation-id``, then the event type). Default
  ``partition_key``.
* ``kafka.dlt.enabled`` / ``kafka.dlt.suffix`` — dead-letter an
  undeserialisable record to ``<topic><suffix>``. Default ``true`` / ``.DLT``.
* ``serialization-format`` — ``json | firefly-json | avro | protobuf``.
* ``redis.url`` — Redis URL. Default ``redis://localhost:6379/0``.
* ``outbox.datasource`` / ``outbox.url`` — the datasource of the ``postgres`` and ``database`` buses: a
  datasource of the registry by name, or its URL (an alias resolved through the registry: an identical URL
  reuses that datasource, another one registers the datasource ``eda``). Default: the primary datasource.
* ``postgres.datasource`` / ``postgres.dsn`` — the same for ``postgres`` (``dsn`` is the key it always had).
* ``postgres.listen-dsn`` — Optional direct DSN for the LISTEN connection (by default it is checked out of
  the datasource's pool).
* ``postgres.channel`` — pg_notify channel. Default ``pyfly_eda``.
* ``postgres.auto-create-tables`` / ``outbox.auto-create-tables`` — Create the outbox tables when they are
  missing (otherwise they are only checked). Unset, the outbox follows ``pyfly.data.relational.ddl-auto`` as
  every framework store does: it creates them under ``create`` and ``create-drop``
  (:func:`~pyfly.data.relational.framework_schema.creates_tables`). When they exist nothing is created, so a
  serving process needs no schema-creation right; ``false`` means the framework never issues DDL for them. The
  Mongo store creates its collections' indexes unless ``outbox.auto-create-tables`` is ``false`` (then it only
  checks them): ``ddl-auto`` is the relational schema's.
* ``outbox.*`` — the outbox buses' relay: ``poll-interval``, ``batch-size``, ``claim-timeout``,
  ``handler-timeout``, ``start`` (``latest``/``earliest``), ``error-strategy``, ``retention.delivered``,
  ``retention.max-age``, ``retention.interval``, ``retention.batch-size``, ``notify`` (see
  :meth:`~pyfly.eda.outbox.OutboxSettings.from_config`).
* ``outbox.enabled`` — make the provider's publisher transactional: a
  :class:`~pyfly.eda.outbox_forwarding.TransactionalEventPublisher` wraps it, so a publish is appended to the
  outbox store in the caller's unit of work and forwarded to the broker once that unit commits, at least once.
  Default ``false``; the ``database`` and ``postgres`` providers are the outbox already.
* ``outbox.store`` — the outbox store of the ``database`` bus and of the transactional publisher: ``sql`` (the
  framework's tables on a relational datasource, ``outbox.datasource``/``outbox.url``), ``mongo``
  (:class:`~pyfly.eda.adapters.mongo_outbox.MongoOutboxStore`: collections of the document database,
  ``pyfly.data.document.database``, on the document datasource's client and units of work; it needs the document
  data layer and a replica set; an ``outbox.datasource``/``outbox.url`` beside it raises) or ``auto`` (the default:
  ``mongo`` when the document datasource is the default of ``@transactional``, which it is when
  ``pyfly.data.document.enabled`` is on and ``pyfly.data.relational.enabled`` is not, unless
  ``pyfly.data.document.transaction.default`` says otherwise, and no ``outbox.datasource``/``outbox.url`` is set;
  else ``sql``). ``auto`` reads the configuration alone: the ``dev`` profile's SQLite fallback and a datasource
  another store registers do not count. The store chosen is logged at INFO (``eda_outbox_store``). The
  ``postgres`` provider is the SQL store on PostgreSQL.
* ``outbox.forward.*`` — the forwarding relay of the transactional publisher: ``destinations`` (comma-separated;
  unset or ``*``: every destination; a publish to another destination goes to the broker after the commit),
  ``group`` (``pyfly.forward:<provider>``), ``poll-interval``, ``batch-size``, ``claim-timeout``,
  ``handler-timeout``, ``start``, ``error-strategy`` and ``retention.*`` (each defaulting to its ``outbox.*``
  value), ``retry.max-attempts``, ``retry.initial-delay``, ``retry.multiplier``, ``retry.max-delay`` (each
  defaulting to ``listener.retry.*``); see :meth:`~pyfly.eda.outbox_forwarding.ForwardingSettings.from_config`.
* ``domain-events.enabled`` — publish the events aggregates raise as their unit of work commits
  (:class:`~pyfly.eda.domain_events.DomainEventPublisher`). Default ``true``.
* ``domain-events.destination`` — also publish them through the event publisher, to this destination (with
  an outbox bus, in the committing unit itself). Default: not published there.
* ``rabbitmq.url`` — AMQP URL. Default ``amqp://guest:guest@localhost/``.
* ``rabbitmq.exchange-name`` — Exchange name. Default ``pyfly``.
* ``rabbitmq.prefetch`` — ``basic.qos`` prefetch of each consumer channel. Default ``20``.
* ``rabbitmq.dead-letter-exchange`` — where an event goes after its last attempt. Default
  ``<exchange-name>.dlx`` (queue ``<group>.<destination>.dlq``).
* ``listener.*`` — the listener container of the Kafka, RabbitMQ and outbox buses: ``transactional``,
  ``datasource``, ``shutdown-timeout``, ``concurrency``, ``retry.max-attempts``, ``retry.initial-delay``,
  ``retry.multiplier``, ``retry.max-delay`` (see
  :meth:`~pyfly.messaging.listener_container.ListenerContainerSettings.from_config`), and
  ``kafka.max-poll-records`` (``100``).

An :class:`~pyfly.eda.dlq.EdaDeadLetterStore` bean, when the application defines one, records every
event a bus dead-letters after its handlers failed on every attempt (the outbox buses write to their own
dead-letter table otherwise).
"""


# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import logging
from typing import Any

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.context.events import ApplicationEventPublisher
from pyfly.core.config import Config
from pyfly.eda.dlq import EdaDeadLetterStore
from pyfly.eda.domain_events import DomainEventPublisher
from pyfly.eda.health import EventPublisherHealthIndicator
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.ports.outbox import OutboxStore

_logger = logging.getLogger(__name__)


@auto_configuration
@conditional_on_property("pyfly.eda.provider")
@conditional_on_missing_bean(EventPublisher)
class EdaAutoConfiguration:
    """Auto-configures the EDA :class:`EventPublisher` from properties."""

    @staticmethod
    def detect_provider() -> str:
        """Pick the strongest broker available at import time."""
        if AutoConfiguration.is_available("aiokafka"):
            return "kafka"
        if AutoConfiguration.is_available("asyncpg"):
            return "postgres"
        if AutoConfiguration.is_available("redis"):
            return "redis"
        if AutoConfiguration.is_available("aio_pika"):
            return "rabbitmq"
        return "memory"

    @bean
    def event_publisher(
        self,
        config: Config,
        dead_letter_store: EdaDeadLetterStore | None = None,
        container: Container | None = None,
    ) -> EventPublisher:
        configured = str(config.get("pyfly.eda.provider", "auto"))
        provider = configured if configured != "auto" else self.detect_provider()

        destinations_raw = str(config.get("pyfly.eda.destinations", "pyfly.events"))
        destinations = [d.strip() for d in destinations_raw.split(",") if d.strip()]
        group = str(config.get("pyfly.eda.group", "pyfly-default"))

        if provider in ("postgres", "database"):
            kind = self._outbox_store_kind(config, provider)  # a store it cannot run on fails here
            return self._outbox_bus(config, provider, destinations, group, dead_letter_store, container, kind)

        transport = self._transport(config, provider, destinations, group, dead_letter_store)
        if not self._outbox_enabled(config):
            return transport
        return self._transactional(config, provider, transport, dead_letter_store, container)

    def _transport(
        self,
        config: Config,
        provider: str,
        destinations: list[str],
        group: str,
        dead_letter_store: EdaDeadLetterStore | None,
    ) -> EventPublisher:
        """The provider's own publisher (a broker, or the in-process bus)."""
        serializer = self._make_serializer(config)

        if provider == "kafka":
            from pyfly.eda.adapters.kafka import KafkaEventBus

            servers = str(config.get("pyfly.eda.kafka.bootstrap-servers", "localhost:9092"))
            key_header = str(config.get("pyfly.eda.kafka.partition-key-header", "partition_key"))
            dlt_enabled = str(config.get("pyfly.eda.kafka.dlt.enabled", "true")).lower() in ("true", "1", "yes")
            dlt_suffix = str(config.get("pyfly.eda.kafka.dlt.suffix", ".DLT")) if dlt_enabled else None
            return KafkaEventBus(
                bootstrap_servers=servers,
                topics=destinations,
                group=group,
                serializer=serializer,
                partition_key_header=key_header,
                dlt_suffix=dlt_suffix,
                settings=self._listener_settings(config),
                dead_letter_store=dead_letter_store,
            )

        if provider == "redis":
            from pyfly.eda.adapters.redis import RedisStreamsEventBus

            url = str(config.get("pyfly.eda.redis.url", "redis://localhost:6379/0"))
            consumer_id = config.get("pyfly.eda.redis.consumer-id")
            return RedisStreamsEventBus(
                url=url,
                streams=destinations,
                group=group,
                consumer_id=str(consumer_id) if consumer_id else None,
                serializer=serializer,
            )

        if provider == "rabbitmq":
            from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus

            url = str(config.get("pyfly.eda.rabbitmq.url", "amqp://guest:guest@localhost/"))
            exchange_name = str(config.get("pyfly.eda.rabbitmq.exchange-name", "pyfly"))
            dead_letter_exchange = config.get("pyfly.eda.rabbitmq.dead-letter-exchange")
            return RabbitMqEventBus(
                url=url,
                exchange_name=exchange_name,
                destinations=destinations,
                group=group,
                serializer=serializer,
                settings=self._listener_settings(config),
                dead_letter_exchange=str(dead_letter_exchange) if dead_letter_exchange else None,
                dead_letter_store=dead_letter_store,
            )

        from pyfly.eda.adapters.memory import InMemoryEventBus

        return InMemoryEventBus()

    @bean
    @conditional_on_property("pyfly.eda.domain-events.enabled", having_value="true", match_if_missing=True)
    @conditional_on_missing_bean(DomainEventPublisher)
    def domain_event_publisher(
        self,
        config: Config,
        event_publisher: EventPublisher,
        events: ApplicationEventPublisher | None = None,
    ) -> DomainEventPublisher:
        """Publishes the events aggregates raise as their unit of work commits, to the application's listeners
        and (with ``pyfly.eda.domain-events.destination``) through the event publisher."""
        destination = str(config.get("pyfly.eda.domain-events.destination", "") or "").strip()
        return DomainEventPublisher(events, event_publisher, destination=destination or None)

    @staticmethod
    def _outbox_enabled(config: Config) -> bool:
        """``pyfly.eda.outbox.enabled`` (default ``false``)."""
        from pyfly.config.properties.data import parse_bool

        raw = config.get("pyfly.eda.outbox.enabled")
        if raw is None or not str(raw).strip():
            return False
        return parse_bool(raw, "pyfly.eda.outbox.enabled")

    @classmethod
    def _outbox_store_kind(cls, config: Config, provider: str) -> str:
        """The outbox store ``pyfly.eda.outbox.store`` names: ``sql`` or ``mongo``; ``auto`` picks one by
        :meth:`_auto_outbox_store`. Logs the store chosen (``eda_outbox_store``, INFO). The ``postgres`` provider is
        the SQL store on PostgreSQL: ``mongo`` raises there, and so does ``mongo`` beside an
        ``outbox.datasource``/``outbox.url``, which name a relational datasource."""
        setting = str(config.get("pyfly.eda.outbox.store", "auto") or "auto").strip().lower()
        if setting not in ("sql", "mongo", "auto"):
            raise ValueError(f"pyfly.eda.outbox.store must be sql, mongo or auto, got {setting!r}")
        if setting == "auto":
            kind, reason = cls._auto_outbox_store(config, provider)
        else:
            kind, reason = setting, f"pyfly.eda.outbox.store={setting}"
        if kind == "mongo" and provider == "postgres":
            raise ValueError(
                "pyfly.eda.provider=postgres keeps the outbox in PostgreSQL (its LISTEN/NOTIFY wake-ups are the SQL "
                "store's), so pyfly.eda.outbox.store=mongo does not apply to it: use pyfly.eda.provider=database for "
                "the outbox bus on MongoDB, or a broker with pyfly.eda.outbox.enabled=true"
            )
        named = cls._outbox_datasource_key(config)
        if kind == "mongo" and named is not None:
            raise ValueError(
                f"pyfly.eda.outbox.store=mongo keeps the outbox in the document database, and {named} names a "
                f"relational datasource for it: remove {named}, or set pyfly.eda.outbox.store=sql (or auto) to keep "
                "the outbox on that datasource"
            )
        _logger.info("eda_outbox_store", extra={"store": kind, "setting": setting, "reason": reason})
        return kind

    @classmethod
    def _auto_outbox_store(cls, config: Config, provider: str) -> tuple[str, str]:
        """The store ``auto`` picks, and why: ``sql`` for the ``postgres`` provider and for an
        ``outbox.datasource``/``outbox.url``; otherwise ``mongo`` exactly when the document datasource is the default
        of ``@transactional`` (:func:`~pyfly.config.properties.mongodb.document_is_default_datasource`: the document
        data layer is on, and the relational one is not, unless ``pyfly.data.document.transaction.default`` says
        otherwise), so that a publish in a plain ``@transactional`` joins that unit. It reads the configuration alone:
        a datasource the ``DataSourceRegistry`` holds without the relational data layer (the ``dev`` profile's
        fallback, another store's URL) does not change the answer, nor does the order the beans are built in."""
        from pyfly.config.properties.mongodb import document_is_default_datasource

        if provider == "postgres":
            return "sql", "pyfly.eda.provider=postgres"
        named = cls._outbox_datasource_key(config)
        if named is not None:
            return "sql", f"{named} is set"
        if document_is_default_datasource(config):
            return "mongo", "the document datasource is the default of @transactional"
        return "sql", "the document datasource is not the default of @transactional"

    @staticmethod
    def _outbox_datasource_key(config: Config) -> str | None:
        """``pyfly.eda.outbox.datasource`` or ``pyfly.eda.outbox.url``, whichever is set (``None``: neither)."""
        for key in ("pyfly.eda.outbox.datasource", "pyfly.eda.outbox.url"):
            value = config.get(key)
            if value is not None and str(value).strip():
                return key
        return None

    @staticmethod
    def _has_mongo_client(config: Config) -> bool:
        """Whether the application has the framework's Mongo client: the document layer is on, as its
        auto-configuration reads ``pyfly.data.document.enabled`` (a ``pyfly.data.document.uri`` alone wires no
        client)."""
        return str(config.get("pyfly.data.document.enabled", "")).strip().lower() == "true"

    @classmethod
    def _transactional(
        cls,
        config: Config,
        provider: str,
        transport: EventPublisher,
        dead_letter_store: EdaDeadLetterStore | None,
        container: Container | None,
    ) -> EventPublisher:
        """*transport* behind the transactional outbox (``pyfly.eda.outbox.enabled``): a
        :class:`~pyfly.eda.outbox_forwarding.TransactionalEventPublisher` on the outbox store ``outbox.store``
        picks (the SQL store of ``outbox.datasource``/``outbox.url``, the primary datasource by default; or the
        Mongo store of the document datasource), forwarding with ``outbox.forward.*``."""
        from pyfly.eda.outbox import SqlOutboxStore
        from pyfly.eda.outbox_forwarding import ForwardingSettings, TransactionalEventPublisher

        kind = cls._outbox_store_kind(config, provider)
        listener = cls._listener_settings(config)
        forwarding = ForwardingSettings.from_config(config, retry=listener.retry)
        store: OutboxStore
        if kind == "mongo":
            store = cls._mongo_outbox_store(config)
        else:
            store = SqlOutboxStore(
                cls._outbox_datasource(config, provider, container),
                create_tables=cls._outbox_creates_tables(config, provider, container),
            )
        return TransactionalEventPublisher(
            transport,
            store,
            name=provider,
            settings=listener,
            dead_letter_store=dead_letter_store,
            **forwarding.forwarder_options(),
        )

    @classmethod
    def _mongo_outbox_store(cls, config: Config) -> OutboxStore:
        """The :class:`~pyfly.eda.adapters.mongo_outbox.MongoOutboxStore` of the application's document datasource
        (``pyfly.data.document.datasource``): its collections in ``pyfly.data.document.database``, on the client and
        units of work of the ``MongoTransactionManager`` the context registers under that name, resolved at each use
        (so an append joins a ``@transactional`` method of the document datasource). It creates its indexes unless
        ``pyfly.eda.outbox.auto-create-tables`` is ``false``."""
        missing = (
            "pyfly.eda.outbox.store=mongo keeps the outbox in the application's MongoDB database, on the client "
            "and units of work of its document datasource: enable the document data layer "
            "(pyfly.data.document.enabled=true, with pyfly.data.document.uri), or run the outbox on a relational "
            "datasource (pyfly.eda.outbox.store=sql)"
        )
        if not cls._has_mongo_client(config):
            raise ValueError(missing)
        try:
            from pyfly.config.properties.mongodb import DocumentProperties
            from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
        except ImportError as error:  # the document extra (pymongo) is not installed
            raise ValueError(
                "pyfly.eda.outbox.store=mongo keeps the outbox in the application's MongoDB database, and the MongoDB "
                "driver is not installed: add the document extra (pip install 'pyfly[data-document]'), or run the "
                "outbox on a relational datasource (pyfly.eda.outbox.store=sql)"
            ) from error
        from pyfly.eda.outbox import OutboxSettings

        properties = DocumentProperties.from_config(config)
        explicit = OutboxSettings.from_config(config).create_tables
        return MongoOutboxStore(
            properties.datasource,
            database=properties.database,
            create_indexes=True if explicit is None else explicit,
        )

    @staticmethod
    def _outbox_creates_tables(config: Config, provider: str, container: Container | None) -> bool:
        """Whether the outbox store creates its missing tables: ``pyfly.eda.outbox.auto-create-tables`` (for
        ``postgres`` also ``pyfly.eda.postgres.auto-create-tables``, which wins) when set; unset, what
        ``pyfly.data.relational.ddl-auto`` lets every framework store do."""
        from pyfly.config.properties.data import parse_bool
        from pyfly.eda.outbox import OutboxSettings

        create_raw = config.get("pyfly.eda.postgres.auto-create-tables") if provider == "postgres" else None
        if create_raw is not None and str(create_raw).strip():
            return parse_bool(create_raw, "pyfly.eda.postgres.auto-create-tables")
        explicit = OutboxSettings.from_config(config).create_tables
        if explicit is not None:
            return explicit
        # Unset: the outbox tables are framework tables, created where ddl-auto lets the stores create theirs.
        from pyfly.data.relational.framework_schema import context_datasource_registry, creates_tables

        return creates_tables(context_datasource_registry(config, container).properties.ddl_auto)

    @classmethod
    def _outbox_bus(
        cls,
        config: Config,
        provider: str,
        destinations: list[str],
        group: str,
        dead_letter_store: EdaDeadLetterStore | None,
        container: Container | None,
        kind: str = "sql",
    ) -> EventPublisher:
        """The ``postgres`` or ``database`` bus: the transactional outbox on a datasource of the context's
        registry (``outbox.datasource``/``outbox.url``, for ``postgres`` also ``postgres.datasource``/
        ``postgres.dsn``; the primary datasource by default), or (*kind* ``mongo``, the ``database`` bus) on the
        Mongo store of the document datasource."""
        from pyfly.eda.outbox import OutboxSettings

        settings = OutboxSettings.from_config(config)
        options: dict[str, Any] = {
            "destinations": destinations,
            "group": group,
            "settings": cls._listener_settings(config),
            "error_strategy": settings.error_strategy,
            "batch_size": settings.batch_size,
            "claim_timeout": settings.claim_timeout,
            "handler_timeout": settings.handler_timeout,
            "retention": settings.retention,
            "start_position": settings.start_position,
            "dead_letter_store": dead_letter_store,
        }
        channel = str(config.get("pyfly.eda.postgres.channel", "pyfly_eda") or "pyfly_eda")
        if kind == "mongo":
            from pyfly.eda.adapters.database import DatabaseEventBus

            return DatabaseEventBus(
                store=cls._mongo_outbox_store(config),
                poll_interval=settings.poll_interval,
                notify=settings.notify,  # true raises at start: the wake-ups are PostgreSQL's
                channel=channel,
                **options,
            )
        datasource = cls._outbox_datasource(config, provider, container)
        create_tables = cls._outbox_creates_tables(config, provider, container)
        listen_raw = config.get("pyfly.eda.postgres.listen-dsn", "")
        listen_dsn = str(listen_raw) if listen_raw else None
        if provider == "postgres":
            from pyfly.eda.adapters.postgres import PostgresEventBus

            return PostgresEventBus(
                datasource=datasource,
                listen_dsn=listen_dsn,
                channel=channel,
                poll_interval_s=settings.poll_interval,
                auto_create_tables=create_tables,
                notify=settings.notify,
                **options,
            )
        from pyfly.eda.adapters.database import DatabaseEventBus

        return DatabaseEventBus(
            datasource,
            poll_interval=settings.poll_interval,
            create_tables=create_tables,
            notify=settings.notify,
            channel=channel,
            listen_dsn=listen_dsn,
            **options,
        )

    @staticmethod
    def _outbox_datasource(config: Config, provider: str, container: Container | None) -> Any:
        from pyfly.data.relational.datasource_registry import DataSourceConfigurationError
        from pyfly.data.relational.framework_schema import context_datasource_registry

        def value(key: str) -> str:
            raw = config.get(key)
            return "" if raw is None else str(raw).strip()

        named_keys = ["pyfly.eda.outbox.datasource"] + (
            ["pyfly.eda.postgres.datasource"] if provider == "postgres" else []
        )
        url_keys = ["pyfly.eda.outbox.url"] + (["pyfly.eda.postgres.dsn"] if provider == "postgres" else [])
        named = next(((key, value(key)) for key in named_keys if value(key)), None)
        url = next(((key, value(key)) for key in url_keys if value(key)), None)
        if named and url:
            raise DataSourceConfigurationError(
                f"{named[0]} and {url[0]} are both set; name the event bus's datasource or give its URL, not both"
            )
        registry = context_datasource_registry(config, container)
        if named:
            return registry.get(named[1])
        if url:
            from pyfly.eda.adapters.postgres import _sqlalchemy_url

            return registry.resolve(_sqlalchemy_url(url[1]), name="eda", url_key=url[0])
        return registry.primary

    @staticmethod
    def _listener_settings(config: Config) -> Any:
        """The Kafka and RabbitMQ listener container settings, from ``pyfly.eda.listener.*``."""
        from pyfly.messaging.listener_container import ListenerContainerSettings

        return ListenerContainerSettings.from_config(config, "pyfly.eda")

    @staticmethod
    def _make_serializer(config: Config) -> Any:
        """Select the event serializer from pyfly.eda.serialization-format (#138).

        json (default) | firefly-json | avro | protobuf — ``firefly-json`` writes the
        LaraFly envelope shape for topics shared with a PHP service (both JSON
        serializers read both shapes); Avro/Protobuf remain opt-in stubs ('bring
        your own' schema), but the selection is now reachable.
        """
        fmt = str(config.get("pyfly.eda.serialization-format", "json")).lower()
        if fmt == "firefly-json":
            from pyfly.eda.serializers import FireflyJsonEventSerializer

            return FireflyJsonEventSerializer()
        if fmt == "avro":
            from pyfly.eda.serializers import AvroEventSerializer

            return AvroEventSerializer()
        if fmt in ("protobuf", "proto"):
            from pyfly.eda.serializers import ProtobufEventSerializer

            return ProtobufEventSerializer()
        from pyfly.eda.serializers import JsonEventSerializer

        return JsonEventSerializer()


@auto_configuration
@conditional_on_class("pyfly.actuator")
@conditional_on_property("pyfly.eda.provider")
class EdaHealthAutoConfiguration:
    """Register the :class:`EventPublisherHealthIndicator` when the actuator is on.

    Registered separately from :class:`EdaAutoConfiguration` so it only
    activates when the actuator subsystem is present. The actuator's
    Starlette adapter auto-discovers any :class:`HealthIndicator` bean
    and adds it to the :class:`HealthAggregator`.
    """

    @bean(name="eda_health")
    def eda_health_indicator(self, event_publisher: EventPublisher) -> EventPublisherHealthIndicator:
        return EventPublisherHealthIndicator(event_publisher)
