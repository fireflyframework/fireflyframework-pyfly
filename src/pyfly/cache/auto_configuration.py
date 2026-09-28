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

from datetime import timedelta
from typing import Any

from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.container import Container
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
    def cache_adapter(self, config: Config, container: Container | None = None) -> CacheAdapter:
        configured = str(config.get("pyfly.cache.provider", "auto"))
        provider = configured if configured != "auto" else self.detect_provider()

        if provider == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.cache.adapters.redis import RedisCacheAdapter

            url = str(config.get("pyfly.cache.redis.url", "redis://localhost:6379/0"))
            client = aioredis.from_url(url)  # type: ignore[no-untyped-call,unused-ignore]
            return RedisCacheAdapter(client=client)

        if provider == "postgres":
            if not AutoConfiguration.is_available("sqlalchemy.ext.asyncio"):
                raise ValueError(
                    "pyfly.cache.provider=postgres requires SQLAlchemy async — "
                    "install pyfly[data-relational,postgresql]."
                )
            from pyfly.cache.adapters.postgres import PostgresCacheAdapter
            from pyfly.data.relational.framework_schema import (
                context_datasource_registry,
                creates_tables,
                module_datasource,
            )

            # pyfly.cache.postgres.datasource names a datasource of the context's registry, and
            # pyfly.cache.postgres.url is an alias resolved through it: neither is the application's
            # primary datasource, an identical URL reuses that datasource's engine.
            registry = context_datasource_registry(config, container)
            datasource = module_datasource(registry, config, "pyfly.cache.postgres", name="cache")
            purge_interval = _purge_interval(config.get("pyfly.cache.postgres.purge-interval", 60))
            return PostgresCacheAdapter(
                datasource,
                create_table=creates_tables(registry.properties.ddl_auto),
                purge_interval=purge_interval if purge_interval > timedelta(0) else None,
            )

        from pyfly.cache.adapters.memory import InMemoryCache

        raw_max_size = config.get("pyfly.cache.max-size", None)
        max_size = int(raw_max_size) if raw_max_size is not None else None
        return InMemoryCache(max_size=max_size)

    @bean
    @conditional_on_property("pyfly.observability.health.enabled", having_value="true", match_if_missing=True)
    def cache_health_indicator(self, cache_adapter: CacheAdapter) -> Any:
        # Registered so /actuator/health reports cache status (audit #74).
        from pyfly.cache.health import CacheHealthIndicator

        return CacheHealthIndicator(adapter=cache_adapter)


def _purge_interval(configured: Any) -> timedelta:
    """``pyfly.cache.postgres.purge-interval``: seconds, or a duration (``90s``, ``500ms``, ``2m``, ``1h``)."""
    from pyfly.resilience.registry import parse_duration

    try:
        return parse_duration(configured)
    except ValueError as exc:
        raise ValueError(
            f"pyfly.cache.postgres.purge-interval must be a number of seconds or a duration such as '90s', "
            f"'500ms', '2m' or '1h' (0 turns the purge on writes off), got {configured!r}"
        ) from exc
