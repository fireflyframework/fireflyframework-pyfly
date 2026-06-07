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

from typing import Any

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
        - ``postgres`` — cross-process via Postgres advisory locks (no extra infra for apps
          already on Postgres).

        The Redis client / SQLAlchemy engine are obtained here (the composition root) and injected;
        the adapters never import their driver at module scope.
        """
        provider = str(config.get("pyfly.scheduling.lock.provider", "none")).lower()
        if provider == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.scheduling.adapters.redis_lock import RedisDistributedLock

            url = str(config.get("pyfly.scheduling.lock.redis.url", "redis://localhost:6379/0"))
            return RedisDistributedLock(aioredis.from_url(url))  # type: ignore[no-untyped-call,unused-ignore]
        if provider == "postgres":
            from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock

            # The AsyncEngine is resolved lazily (first acquire) to avoid bean-ordering issues.
            def _engine() -> Any:
                from sqlalchemy.ext.asyncio import AsyncEngine

                return container.resolve(AsyncEngine)

            return PostgresAdvisoryLock(_engine)
        if provider == "memory":
            return InProcessDistributedLock()
        return LocalLock()

    @bean
    def task_scheduler(self, container: Container) -> TaskScheduler:
        # Resolve the DistributedLock bean above for @scheduled(lock=...) coordination;
        # fall back to the scheduler's own LocalLock if (unexpectedly) absent.
        try:
            lock = container.resolve(DistributedLock)  # type: ignore[type-abstract]
        except (NoSuchBeanError, NoUniqueBeanError):
            lock = None
        return TaskScheduler(lock=lock)
