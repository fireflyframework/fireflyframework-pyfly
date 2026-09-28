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
"""Scheduling auto-configuration — TaskScheduler bean."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

try:
    from pyfly.scheduling.task_scheduler import TaskScheduler
except ImportError:
    TaskScheduler = object  # type: ignore[misc,assignment]

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError, NoUniqueBeanError
from pyfly.context.conditions import auto_configuration, conditional_on_class
from pyfly.core.config import Config
from pyfly.scheduling.lock import DistributedLock, InProcessDistributedLock, LocalLock
from pyfly.scheduling.ports.outbound import TaskExecutorPort


@auto_configuration
@conditional_on_class("croniter")
class SchedulingAutoConfiguration:
    """Auto-configures a TaskScheduler bean (and its distributed lock) when croniter is installed."""

    @bean
    def distributed_lock(self, config: Config, container: Container) -> DistributedLock:
        """Select the @scheduled lock backend (Spring/ShedLock parity).

        ``pyfly.scheduling.lock.provider``:
        - ``none`` (default) — no coordination (LocalLock);
        - ``memory`` — single-process mutual exclusion;
        - ``redis`` — cross-process via Redis SET NX PX;
        - ``database`` — cross-process via the portable lease table ``pyfly_locks``
          (:class:`LeaseLock`), on any relational backend: it honors ``lock_ttl`` and holds no
          connection while the job runs;
        - ``postgres`` — the same lease table; with ``pyfly.scheduling.lock.postgres.advisory=true``,
          Postgres session-level advisory locks instead (:class:`PostgresAdvisoryLock`, an opt-in
          accelerator: the server releases the lock the moment its holder disconnects).

        A database lock runs on the datasource named by ``pyfly.scheduling.lock.datasource`` (or given by
        ``pyfly.scheduling.lock.url``), by default the primary, looked up in the context's datasource
        registry; the lease table is created at start when ``pyfly.data.relational.ddl-auto`` allows it
        (:func:`~pyfly.data.relational.framework_schema.creates_tables`). The Redis client / datasource are
        obtained here (the composition root) and injected; the adapters never import their driver at module
        scope.
        """
        provider = str(config.get("pyfly.scheduling.lock.provider", "none")).lower()
        if provider == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.scheduling.adapters.redis_lock import RedisDistributedLock

            url = str(config.get("pyfly.scheduling.lock.redis.url", "redis://localhost:6379/0"))
            return RedisDistributedLock(aioredis.from_url(url))  # type: ignore[no-untyped-call,unused-ignore]
        if provider in ("database", "postgres"):
            from pyfly.data.relational.datasource_registry import DataSourceConfigurationError
            from pyfly.data.relational.framework_schema import (
                context_datasource_registry,
                creates_tables,
                module_datasource,
            )

            registry = context_datasource_registry(config, container)
            datasource = module_datasource(registry, config, "pyfly.scheduling.lock", name="scheduling-lock")
            advisory = str(config.get("pyfly.scheduling.lock.postgres.advisory", "false")).strip().lower() == "true"
            if provider == "postgres" and advisory:
                from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock

                if datasource.capabilities.dialect != "postgresql":
                    raise DataSourceConfigurationError(
                        "pyfly.scheduling.lock.postgres.advisory=true needs a PostgreSQL datasource, but "
                        f"'{datasource.name}' is {datasource.capabilities.dialect}; use "
                        "pyfly.scheduling.lock.provider=database for the portable lease table"
                    )
                return PostgresAdvisoryLock(datasource)
            from pyfly.scheduling.adapters.lease_lock import LeaseLock

            return LeaseLock(datasource, create_table=creates_tables(registry.properties.ddl_auto))
        if provider == "memory":
            return InProcessDistributedLock()
        return LocalLock()

    @bean
    def task_scheduler(self, container: Container, config: Config) -> TaskScheduler:
        # Resolve the DistributedLock bean above for @scheduled(lock=...) coordination;
        # fall back to the scheduler's own LocalLock if (unexpectedly) absent.
        try:
            lock = container.resolve(DistributedLock)  # type: ignore[type-abstract]
        except (NoSuchBeanError, NoUniqueBeanError):
            lock = None

        # Executor backend: pyfly.scheduling.executor.type = 'asyncio' (default, in-loop tasks)
        # or 'thread' (offload blocking jobs to a pool of pyfly.scheduling.executor.max-workers).
        executor: TaskExecutorPort | None = None
        if str(config.get("pyfly.scheduling.executor.type", "asyncio")).lower() == "thread":
            from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor

            executor = ThreadPoolTaskExecutor(max_workers=int(config.get("pyfly.scheduling.executor.max-workers", 4)))
        return TaskScheduler(executor=executor, lock=lock)
