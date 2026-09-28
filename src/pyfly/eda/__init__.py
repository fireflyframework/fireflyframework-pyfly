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
"""PyFly EDA — Event-Driven Architecture.

Import concrete adapter types from the adapter package::

    from pyfly.eda.adapters.memory import InMemoryEventBus
    from pyfly.eda.adapters.database import DatabaseEventBus

The transactional outbox the ``postgres`` and ``database`` buses (and ``eventsourcing.TransactionalOutbox``)
run on is :mod:`pyfly.eda.outbox`, on the :class:`~pyfly.eda.ports.outbox.OutboxStore` port; the layer that makes
any other provider's publisher transactional (``pyfly.eda.outbox.enabled``) is :mod:`pyfly.eda.outbox_forwarding`;
the publication of aggregates' domain events at commit is :mod:`pyfly.eda.domain_events`.
"""

from pyfly.eda.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitOpenError,
    EventCircuitBreaker,
)
from pyfly.eda.decorators import event_listener, event_publisher, publish_result
from pyfly.eda.dlq import (
    EdaDeadLetterEntry,
    EdaDeadLetterStore,
    InMemoryEdaDeadLetterStore,
    SqlEdaDeadLetterStore,
)
from pyfly.eda.filter import EventFilter, HeaderEventFilter, PredicateEventFilter
from pyfly.eda.ports.outbound import EventHandler, EventPublisher
from pyfly.eda.ports.outbox import OutboxStore
from pyfly.eda.serializers import (
    AvroEventSerializer,
    EventSerializer,
    JsonEventSerializer,
    ProtobufEventSerializer,
)
from pyfly.eda.types import ErrorStrategy, EventEnvelope

__all__ = [
    "AvroEventSerializer",
    "CircuitBreakerConfig",
    "CircuitOpenError",
    "EdaDeadLetterEntry",
    "EdaDeadLetterStore",
    "ErrorStrategy",
    "EventCircuitBreaker",
    "EventEnvelope",
    "EventFilter",
    "EventHandler",
    "EventPublisher",
    "EventSerializer",
    "HeaderEventFilter",
    "InMemoryEdaDeadLetterStore",
    "JsonEventSerializer",
    "OutboxStore",
    "PredicateEventFilter",
    "ProtobufEventSerializer",
    "SqlEdaDeadLetterStore",
    "event_listener",
    "event_publisher",
    "publish_result",
]
