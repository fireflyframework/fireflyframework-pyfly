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
* ``postgres`` — :class:`PostgresEventBus`, when ``asyncpg`` is available.

Configuration keys (all optional, prefix ``pyfly.eda.``):

* ``provider`` — ``memory | kafka | redis | postgres | auto``.
* ``destinations`` — comma-separated list of topics / streams /
  destinations to consume from. Defaults to ``pyfly.events``.
* ``group`` — consumer group / cursor name. Defaults to
  ``pyfly-default``.
* ``kafka.bootstrap-servers`` — Kafka bootstrap. Default ``localhost:9092``.
* ``redis.url`` — Redis URL. Default ``redis://localhost:6379/0``.
* ``postgres.dsn`` — Postgres DSN for the producer pool.
* ``postgres.listen-dsn`` — Optional dedicated DSN for the LISTEN
  connection. Defaults to ``postgres.dsn``.
* ``postgres.channel`` — pg_notify channel. Default ``pyfly_eda``.
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.eda.health import EventPublisherHealthIndicator
from pyfly.eda.ports.outbound import EventPublisher


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
        return "memory"

    @bean
    def event_publisher(self, config: Config) -> EventPublisher:
        configured = str(config.get("pyfly.eda.provider", "auto"))
        provider = configured if configured != "auto" else self.detect_provider()

        destinations_raw = str(config.get("pyfly.eda.destinations", "pyfly.events"))
        destinations = [d.strip() for d in destinations_raw.split(",") if d.strip()]
        group = str(config.get("pyfly.eda.group", "pyfly-default"))

        if provider == "kafka":
            from pyfly.eda.adapters.kafka import KafkaEventBus

            servers = str(config.get("pyfly.eda.kafka.bootstrap-servers", "localhost:9092"))
            return KafkaEventBus(
                bootstrap_servers=servers,
                topics=destinations,
                group=group,
            )

        if provider == "redis":
            from pyfly.eda.adapters.redis import RedisStreamsEventBus

            url = str(config.get("pyfly.eda.redis.url", "redis://localhost:6379/0"))
            return RedisStreamsEventBus(
                url=url,
                streams=destinations,
                group=group,
            )

        if provider == "postgres":
            from pyfly.eda.adapters.postgres import PostgresEventBus

            dsn = str(config.get("pyfly.eda.postgres.dsn", ""))
            if not dsn:
                msg = "pyfly.eda.postgres.dsn is required when provider=postgres"
                raise ValueError(msg)
            listen_dsn_raw = config.get("pyfly.eda.postgres.listen-dsn", "")
            listen_dsn = str(listen_dsn_raw) if listen_dsn_raw else None
            channel = str(config.get("pyfly.eda.postgres.channel", "pyfly_eda"))
            return PostgresEventBus(
                dsn=dsn,
                listen_dsn=listen_dsn,
                channel=channel,
                destinations=destinations,
                group=group,
            )

        from pyfly.eda.adapters.memory import InMemoryEventBus

        return InMemoryEventBus()


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
    def eda_health_indicator(
        self, event_publisher: EventPublisher
    ) -> EventPublisherHealthIndicator:
        return EventPublisherHealthIndicator(event_publisher)
