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
"""``HealthIndicator`` for SQLAlchemy async engines.

Pings each database with a portable ``SELECT 1`` (``select(literal(1))``, which Oracle renders with
``FROM DUAL``) and reports the dialect on the ``details`` payload so the actuator response makes it
obvious what is being checked.

The indicator is built for the Kubernetes readiness probe, and only for it:

- it declares ``probe_groups = {READINESS}``, so a database blip takes the pod out of the load
  balancer instead of failing liveness and restarting every replica at once;
- each check is bounded by ``timeout`` (2 s by default, ``pyfly.data.relational.health.timeout``), so
  a silent database answers DOWN in time instead of hanging the probe;
- when the pool has no idle connection and no overflow left, the check does not queue behind the
  application for ``pool_timeout``: it reports ``UNKNOWN`` (validation skipped), which keeps the
  aggregate UP;
- with a registry it checks every datasource (primary, replicas, named, module datasources)
  concurrently and reports each under ``details["datasources"]``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pyfly.actuator.health import HealthStatus, ProbeGroup, aggregate_status


class SqlAlchemyHealthIndicator:
    """Database health probe — ``UP`` iff ``SELECT 1`` succeeds within the timeout on every datasource."""

    probe_groups: ClassVar[frozenset[ProbeGroup]] = frozenset({ProbeGroup.READINESS})

    def __init__(self, engine: Any, *, registry: Any = None, timeout: float = 2.0) -> None:
        self._engine = engine
        self._registry = registry
        self._timeout = timeout

    async def health(self) -> HealthStatus:
        dialect = _dialect(self._engine)
        targets = self._targets()
        if len(targets) == 1 and targets[0][1] is self._engine:
            return await self._check(self._engine)
        results = await asyncio.gather(*(self._check(engine) for _, engine in targets))
        datasources = {
            label: {"status": result.status, **result.details}
            for (label, _), result in zip(targets, results, strict=True)
        }
        status = aggregate_status([result.status for result in results])
        return HealthStatus(status=status, details={"database": dialect, "datasources": datasources})

    def _targets(self) -> list[tuple[str, Any]]:
        if self._registry is None:
            return [("primary", self._engine)]
        targets = [(datasource.qualified_name, datasource.engine) for datasource in self._registry.all_datasources()]
        return targets or [("primary", self._engine)]

    async def _check(self, engine: Any) -> HealthStatus:
        from sqlalchemy import literal, select

        dialect = _dialect(engine)
        if _pool_exhausted(engine):
            return HealthStatus(
                status="UNKNOWN",
                details={"database": dialect, "validation": "skipped: pool exhausted"},
            )
        try:
            async with asyncio.timeout(self._timeout), engine.connect() as conn:
                await conn.execute(select(literal(1)))
        except TimeoutError:
            return HealthStatus(
                status="DOWN",
                details={
                    "database": dialect,
                    "error": "TimeoutError",
                    "message": f"no answer within {self._timeout:g} s",
                },
            )
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"database": dialect, "error": type(exc).__name__, "message": _masked(engine, exc)[:200]},
            )
        return HealthStatus(status="UP", details={"database": dialect})


def _dialect(engine: Any) -> str:
    return str(getattr(getattr(engine, "dialect", None), "name", "unknown"))


def _pool_exhausted(engine: Any) -> bool:
    """Whether a checkout would have to wait: no idle connection and no overflow left."""
    from sqlalchemy.pool import QueuePool

    pool = getattr(getattr(engine, "sync_engine", None), "pool", None)
    if not isinstance(pool, QueuePool):
        return False
    max_overflow = int(getattr(pool, "_max_overflow", 0))
    if max_overflow < 0:  # unlimited overflow never runs out
        return False
    return pool.checkedin() == 0 and pool.overflow() >= max_overflow


def _masked(engine: Any, exc: BaseException) -> str:
    """The exception text with the URL's password masked, should a driver echo it."""
    message = str(exc)
    password = getattr(getattr(engine, "url", None), "password", None)
    return message.replace(password, "***") if password else message
