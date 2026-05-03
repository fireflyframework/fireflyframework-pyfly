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
"""PyFly Messaging — Message broker abstraction with pluggable adapters.

Import concrete adapter types from the adapter package::

    from pyfly.messaging.adapters.memory import InMemoryMessageBroker
    from pyfly.messaging.adapters.kafka import KafkaAdapter
"""

from pyfly.messaging.decorators import message_listener
from pyfly.messaging.ports.outbound import MessageBrokerPort, MessageHandler
from pyfly.messaging.types import Message

__all__ = [
    "Message",
    "MessageBrokerPort",
    "MessageHandler",
    "message_listener",
]
