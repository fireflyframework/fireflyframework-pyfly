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
"""Session subsystem auto-configuration."""

from __future__ import annotations

from typing import Any

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.filter import SessionFilter
from pyfly.session.ports.outbound import SessionStore


@auto_configuration
@conditional_on_property("pyfly.session.enabled", having_value="true")
@conditional_on_missing_bean(SessionStore)
class SessionStoreAutoConfiguration:
    """Auto-configures the session store based on provider detection."""

    @bean
    def session_store(self, config: Config) -> SessionStore:
        store_type = str(config.get("pyfly.session.store", "memory"))

        if store_type == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.session.adapters.redis import RedisSessionStore

            url = str(config.get("pyfly.session.redis.url", "redis://localhost:6379/0"))
            client = aioredis.from_url(url)  # type: ignore[no-untyped-call,unused-ignore]
            return RedisSessionStore(client=client)

        from pyfly.session.adapters.memory import InMemorySessionStore

        return InMemorySessionStore()


@auto_configuration
@conditional_on_property("pyfly.session.enabled", having_value="true")
class SessionFilterAutoConfiguration:
    """Auto-configures the SessionFilter when sessions are enabled."""

    @bean
    def session_filter(self, config: Config, session_store: SessionStore) -> SessionFilter:
        cookie_name = str(config.get("pyfly.session.cookie-name", "PYFLY_SESSION"))
        ttl = int(config.get("pyfly.session.ttl", 1800))
        secure = str(config.get("pyfly.session.cookie.secure", "false")).lower() in ("true", "1", "yes")
        return SessionFilter(store=session_store, cookie_name=cookie_name, ttl=ttl, secure=secure)


@auto_configuration
@conditional_on_property("pyfly.session.concurrency.enabled", having_value="true")
class SessionConcurrencyAutoConfiguration:
    """Auto-configures per-principal session concurrency control (Spring maximumSessions)."""

    @bean
    def session_concurrency_controller(
        self, config: Config, session_store: SessionStore, container: Container
    ) -> SessionConcurrencyController:
        from pyfly.session.concurrency import (
            ConcurrencyControlPolicy,
            InMemorySessionRegistry,
            SessionRegistry,
        )

        policy = ConcurrencyControlPolicy(
            max_sessions=int(config.get("pyfly.session.concurrency.max-sessions", -1)),
            strategy=str(config.get("pyfly.session.concurrency.strategy", "evict-oldest")),
        )
        # Registry backend: 'memory' (default, single-instance), 'redis' (cross-process), or
        # 'postgres' (durable + cross-process, no Redis needed). The Redis client / SQLAlchemy
        # engine are obtained here (the composition root) and injected — the adapters never
        # import their driver at module scope.
        registry: SessionRegistry
        registry_type = str(config.get("pyfly.session.concurrency.registry", "memory")).lower()
        if registry_type == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.session.adapters.redis_registry import RedisSessionRegistry

            url = str(
                config.get("pyfly.session.concurrency.redis.url")
                or config.get("pyfly.session.redis.url", "redis://localhost:6379/0")
            )
            registry = RedisSessionRegistry(aioredis.from_url(url))  # type: ignore[no-untyped-call,unused-ignore]
        elif registry_type == "postgres":
            from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry

            def _engine() -> Any:
                from sqlalchemy.ext.asyncio import AsyncEngine

                return container.resolve(AsyncEngine)

            registry = PostgresSessionRegistry(_engine)
        else:
            registry = InMemorySessionRegistry()
        return SessionConcurrencyController(registry, policy, session_deleter=session_store.delete)
