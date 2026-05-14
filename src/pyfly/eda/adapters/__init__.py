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
"""EDA adapters — concrete implementations of event-driven ports.

Concrete brokers are imported lazily so the adapter package stays
importable even when the optional broker libraries (aiokafka, redis,
asyncpg) are not installed. Reach the implementations through their
fully-qualified module path::

    from pyfly.eda.adapters.kafka import KafkaEventBus
    from pyfly.eda.adapters.postgres import PostgresEventBus
    from pyfly.eda.adapters.redis import RedisStreamsEventBus
"""

from pyfly.eda.adapters.memory import InMemoryEventBus

__all__ = ["InMemoryEventBus"]
