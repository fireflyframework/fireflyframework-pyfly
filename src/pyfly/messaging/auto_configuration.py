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
"""Messaging subsystem auto-configuration."""

from __future__ import annotations

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
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
            from pyfly.messaging.adapters.kafka import KafkaAdapter

            servers = str(config.get("pyfly.messaging.kafka.bootstrap-servers", "localhost:9092"))
            return KafkaAdapter(bootstrap_servers=servers)

        if provider == "rabbitmq":
            from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

            url = str(config.get("pyfly.messaging.rabbitmq.url", "amqp://guest:guest@localhost/"))
            return RabbitMQAdapter(url=url)

        from pyfly.messaging.adapters.memory import InMemoryMessageBroker

        return InMemoryMessageBroker()
