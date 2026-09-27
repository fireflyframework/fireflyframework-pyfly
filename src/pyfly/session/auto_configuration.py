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

import logging
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

logger = logging.getLogger(__name__)

_STORES = ("memory", "redis", "postgres")
_REGISTRIES = ("memory", "redis", "postgres")


def _backend(config: Config, key: str, accepted: tuple[str, ...]) -> str:
    """The backend *key* names (case-insensitive), or a ``ValueError`` naming the accepted values: an unknown
    value used to fall back to the in-memory backend silently."""
    value = str(config.get(key, "memory") or "memory").strip().lower()
    if value not in accepted:
        raise ValueError(f"{key} must be one of {', '.join(accepted)}, got {value!r}")
    return value


def _relational_datasource(config: Config, container: Container | None, prefix: str, *, name: str) -> tuple[Any, bool]:
    """The datasource of a SQL session component, from the context's ``DataSourceRegistry``
    (``<prefix>.datasource`` names one, ``<prefix>.url`` is an alias resolved through the registry, and with
    neither it is the primary), and whether the component may create its tables."""
    from pyfly.data.relational.framework_schema import context_datasource_registry, creates_tables, module_datasource

    registry = context_datasource_registry(config, container)
    return module_datasource(registry, config, prefix, name=name), creates_tables(registry.properties.ddl_auto)


@auto_configuration
@conditional_on_property("pyfly.session.enabled", having_value="true")
@conditional_on_missing_bean(SessionStore)
class SessionStoreAutoConfiguration:
    """Auto-configures the session store: ``pyfly.session.store`` is ``memory`` (the default), ``redis`` or
    ``postgres`` (:class:`~pyfly.session.adapters.sql_session_store.SqlSessionStore`, on any SQL backend: the
    datasource ``pyfly.session.postgres.datasource`` names, or the one ``pyfly.session.postgres.url`` resolves
    to, or the primary)."""

    @bean
    def session_store(self, config: Config, container: Container | None = None) -> SessionStore:
        store_type = _backend(config, "pyfly.session.store", _STORES)

        if store_type == "redis":
            if AutoConfiguration.is_available("redis.asyncio"):
                import redis.asyncio as aioredis

                from pyfly.session.adapters.redis import RedisSessionStore

                url = str(config.get("pyfly.session.redis.url", "redis://localhost:6379/0"))
                client = aioredis.from_url(url)  # type: ignore[no-untyped-call,unused-ignore]
                return RedisSessionStore(client=client)
            logger.warning("session_store_fallback: pyfly.session.store=redis but redis.asyncio is not installed")

        if store_type == "postgres":
            from pyfly.session.adapters.sql_session_store import SqlSessionStore

            datasource, create = _relational_datasource(config, container, "pyfly.session.postgres", name="session")
            return SqlSessionStore(datasource, create_table=create)

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
        # 'postgres' (durable + cross-process, no Redis needed, on any SQL backend). The Redis client /
        # datasource are obtained here (the composition root) and injected — the adapters never
        # import their driver at module scope.
        registry: SessionRegistry
        registry_type = _backend(config, "pyfly.session.concurrency.registry", _REGISTRIES)
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

            datasource, create = _relational_datasource(
                config, container, "pyfly.session.concurrency.postgres", name="session-registry"
            )
            ttl = int(config.get("pyfly.session.ttl", 1800))
            registry = PostgresSessionRegistry(datasource, ttl=ttl, create_table=create)
        else:
            registry_type = "memory"
            registry = InMemorySessionRegistry()
        _report_a_process_local_store(registry_type, session_store)
        return SessionConcurrencyController(
            registry, policy, session_deleter=session_store.delete, session_store=session_store
        )


def _report_a_process_local_store(registry_type: str, session_store: SessionStore) -> None:
    """A registry shared by the instances beside a store each instance keeps to itself: the cap counts every
    instance's sessions, but evicting one another instance holds does not end it there."""
    from pyfly.session.adapters.memory import InMemorySessionStore

    if registry_type != "memory" and isinstance(session_store, InMemorySessionStore):
        logger.warning(
            "session_registry_not_shared: pyfly.session.concurrency.registry=%s is shared by every instance, "
            "but the session store is in memory: evicting a session another instance holds leaves it usable "
            "there. Share the sessions too (pyfly.session.store=postgres or redis).",
            registry_type,
        )
