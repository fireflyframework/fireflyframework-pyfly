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
"""

from pyfly.eda.decorators import event_listener, event_publisher, publish_result
from pyfly.eda.ports.outbound import EventHandler, EventPublisher
from pyfly.eda.types import ErrorStrategy, EventEnvelope

__all__ = [
    "ErrorStrategy",
    "EventEnvelope",
    "EventHandler",
    "EventPublisher",
    "event_listener",
    "event_publisher",
    "publish_result",
]
