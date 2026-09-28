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
"""Messaging subsystem auto-configuration.

Registers the :class:`MessageBrokerPort` bean keyed on ``pyfly.messaging.provider`` (``kafka``,
``rabbitmq``, ``memory`` or ``auto``). The Kafka and RabbitMQ adapters get their listener container
settings from ``pyfly.messaging.listener.*`` (see
:meth:`~pyfly.messaging.listener_container.ListenerContainerSettings.from_config`), plus:

* ``kafka.bootstrap-servers`` (``localhost:9092``), ``kafka.auto-offset-reset`` (``latest``),
  ``kafka.dlt.enabled`` (``true``), ``kafka.dlt.suffix`` (``.DLT``) and ``kafka.max-poll-records``
  (``100``);
* ``rabbitmq.url``, ``rabbitmq.prefetch`` (``20``) and ``rabbitmq.dead-letter-exchange``
  (``pyfly.dlx``).
"""

from __future__ import annotations

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.messaging.listener_container import ListenerContainerSettings
from pyfly.messaging.ports.outbound import MessageBrokerPort


@auto_configuration
@conditional_on_property("pyfly.messaging.provider")
@conditional_on_missing_bean(MessageBrokerPort)
class MessagingAutoConfiguration:
    """Auto-configures the message broker based on provider detection."""

    @staticmethod
    def detect_provider() -> str:
        """Detect the best available messaging provider."""
        if AutoConfiguration.is_available("aiokafka"):
            return "kafka"
        if AutoConfiguration.is_available("aio_pika"):
            return "rabbitmq"
        return "memory"

    @bean
    def message_broker(self, config: Config) -> MessageBrokerPort:
        configured = str(config.get("pyfly.messaging.provider", "auto"))
        provider = configured if configured != "auto" else self.detect_provider()

        if provider == "kafka":
            from pyfly.messaging.adapters.kafka import DEFAULT_DLT_SUFFIX, KafkaAdapter

            servers = str(config.get("pyfly.messaging.kafka.bootstrap-servers", "localhost:9092"))
            dlt_enabled = str(config.get("pyfly.messaging.kafka.dlt.enabled", "true")).lower() in ("true", "1", "yes")
            return KafkaAdapter(
                bootstrap_servers=servers,
                settings=ListenerContainerSettings.from_config(config, "pyfly.messaging"),
                auto_offset_reset=str(config.get("pyfly.messaging.kafka.auto-offset-reset", "latest")),
                dead_letter_suffix=(
                    str(config.get("pyfly.messaging.kafka.dlt.suffix", DEFAULT_DLT_SUFFIX)) if dlt_enabled else None
                ),
            )

        if provider == "rabbitmq":
            from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

            url = str(config.get("pyfly.messaging.rabbitmq.url", "amqp://guest:guest@localhost/"))
            dead_letter_exchange = config.get("pyfly.messaging.rabbitmq.dead-letter-exchange")
            return RabbitMQAdapter(
                url=url,
                settings=ListenerContainerSettings.from_config(config, "pyfly.messaging"),
                dead_letter_exchange=str(dead_letter_exchange) if dead_letter_exchange else None,
            )

        from pyfly.messaging.adapters.memory import InMemoryMessageBroker

        return InMemoryMessageBroker()
