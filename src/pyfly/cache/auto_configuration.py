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
"""Cache subsystem auto-configuration."""

from __future__ import annotations

from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config


@auto_configuration
@conditional_on_property("pyfly.cache.enabled", having_value="true")
@conditional_on_missing_bean(CacheAdapter)
class CacheAutoConfiguration:
    """Auto-configures the cache adapter based on provider detection."""

    @staticmethod
    def detect_provider() -> str:
        """Detect the best available cache provider."""
        if AutoConfiguration.is_available("redis.asyncio"):
            return "redis"
        return "memory"

    @bean
    def cache_adapter(self, config: Config) -> CacheAdapter:
        configured = str(config.get("pyfly.cache.provider", "auto"))
        provider = configured if configured != "auto" else self.detect_provider()

        if provider == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.cache.adapters.redis import RedisCacheAdapter

            url = str(config.get("pyfly.cache.redis.url", "redis://localhost:6379/0"))
            client = aioredis.from_url(url)  # type: ignore[no-untyped-call,unused-ignore]
            return RedisCacheAdapter(client=client)

        from pyfly.cache.adapters.memory import InMemoryCache

        return InMemoryCache()
