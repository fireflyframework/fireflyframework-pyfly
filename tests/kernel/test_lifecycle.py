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
"""Tests for unified Lifecycle protocol."""

from __future__ import annotations

from pyfly.kernel.lifecycle import Lifecycle


class TestLifecycleProtocol:
    def test_messaging_adapters_are_lifecycle(self):
        from pyfly.messaging.adapters.kafka import KafkaAdapter
        from pyfly.messaging.adapters.memory import InMemoryMessageBroker
        from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

        assert issubclass(KafkaAdapter, Lifecycle)
        assert issubclass(InMemoryMessageBroker, Lifecycle)
        assert issubclass(RabbitMQAdapter, Lifecycle)

    def test_cache_adapters_are_lifecycle(self):
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.cache.adapters.redis import RedisCacheAdapter

        assert issubclass(InMemoryCache, Lifecycle)
        assert issubclass(RedisCacheAdapter, Lifecycle)

    def test_client_adapters_are_lifecycle(self):
        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

        assert issubclass(HttpxClientAdapter, Lifecycle)

    def test_eda_adapters_are_lifecycle(self):
        from pyfly.eda.adapters.memory import InMemoryEventBus

        assert issubclass(InMemoryEventBus, Lifecycle)

    def test_scheduling_adapters_are_lifecycle(self):
        from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
        from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor

        assert issubclass(AsyncIOTaskExecutor, Lifecycle)
        assert issubclass(ThreadPoolTaskExecutor, Lifecycle)

    def test_lifecycle_protocol_is_runtime_checkable(self):
        """Lifecycle can be used with isinstance() at runtime."""
        from pyfly.cache.adapters.memory import InMemoryCache

        cache = InMemoryCache()
        assert isinstance(cache, Lifecycle)

    def test_non_lifecycle_class_fails_check(self):
        """Plain class without start/stop doesn't satisfy Lifecycle."""

        class NotLifecycle:
            pass

        assert not isinstance(NotLifecycle(), Lifecycle)
