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
- each check answers within ``timeout`` (2 s by default, ``pyfly.data.relational.health.timeout``),
  whatever the driver does. The ``SELECT 1`` runs in a task of its own, and the probe stops waiting
  for it at the deadline: a database that went silent on an already pooled connection would otherwise
  keep the probe waiting in the driver's cleanup (asyncpg opens a new connection to cancel the query,
  and waits for it without a timeout);
- the late check does not keep its connection. Its socket is closed on the spot (the dialect's
  ``terminate``, which sends nothing and waits for nothing) and the check is cancelled, so it gives its
  pool slot back at once. A middlebox that forgot an idle flow (a cloud NAT or load balancer after its
  idle timeout) black-holes one pooled connection while the database accepts new ones; without this
  the check would sit in the driver's cleanup until the kernel gave up on the socket (about 15 minutes
  on Linux), and the datasource would stay DOWN all that time. With it, one probe answers DOWN and the
  next one runs on a fresh connection. A check on a connection the pool shares with the application
  (``StaticPool`` for SQLite ``:memory:``) is neither closed nor cancelled, only no longer waited for:
  it runs after the application's statement ahead of it;
- while a check that missed its deadline is still winding down, the next probe of that datasource
  answers DOWN at once instead of borrowing another connection, so stuck checks cannot pile up;
- a probe that is itself cancelled (the client hung up) stops the check only when no other probe is
  waiting for it;
- when the pool has no idle connection and no overflow left, the check does not queue behind the
  application for ``pool_timeout``: it reports ``UNKNOWN`` (validation skipped), which keeps the
  aggregate UP. A pod whose every connection is stuck in application work therefore stays in
  rotation; the pool metrics (``pyfly_db_pool_checked_out``, ``pyfly_db_pool_acquire_seconds``) are
  the signal for that case;
- with a registry it checks every datasource (primary, replicas, named, module datasources)
  concurrently and reports each under ``details["datasources"]``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from pyfly.actuator.health import HealthStatus, ProbeGroup, aggregate_status

if TYPE_CHECKING:
    from sqlalchemy.pool import PoolProxiedConnection

_logger = logging.getLogger(__name__)


class _Check:
    """One ``SELECT 1`` in flight on an engine, shared by the probes that arrive while it runs."""

    __slots__ = ("abandoned", "connection", "engine", "started", "task", "waiters")

    task: asyncio.Task[HealthStatus]

    def __init__(self, engine: Any, started: float) -> None:
        self.engine = engine
        self.started = started
        self.abandoned = False
        # The pooled connection the check holds, once it has one. Its ``dbapi_connection`` turns None
        # when the connection goes back to the pool, so a late terminate never hits a connection that
        # someone else may have borrowed since.
        self.connection: PoolProxiedConnection | None = None
        # Probes waiting for this check; a cancelled probe stops the check only when it was the last.
        self.waiters = 0


class SqlAlchemyHealthIndicator:
    """Database health probe — ``UP`` iff ``SELECT 1`` succeeds within the timeout on every datasource."""

    probe_groups: ClassVar[frozenset[ProbeGroup]] = frozenset({ProbeGroup.READINESS})

    def __init__(self, engine: Any, *, registry: Any = None, timeout: float = 2.0) -> None:
        self._engine = engine
        self._registry = registry
        self._timeout = timeout
        # The check in flight per engine (keyed by identity); an entry leaves when its task finishes.
        self._checks: dict[int, _Check] = {}

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
        """The outcome of ``SELECT 1`` on *engine*, within the timeout whatever the driver does."""
        dialect = _dialect(engine)
        loop = asyncio.get_running_loop()
        check = self._checks.get(id(engine))
        if check is not None and check.task.get_loop() is not loop:
            check = None  # left behind by an event loop that is gone
        if check is not None and check.abandoned:
            return HealthStatus(
                status="DOWN",
                details={
                    "database": dialect,
                    "error": "TimeoutError",
                    "message": (
                        f"previous check still running after {loop.time() - check.started:.1f} s "
                        "(no connection borrowed)"
                    ),
                },
            )
        if check is None:
            if _pool_exhausted(engine):
                return HealthStatus(
                    status="UNKNOWN",
                    details={"database": dialect, "validation": "skipped: pool exhausted"},
                )
            check = self._start(engine, loop)
        remaining = max(self._timeout - (loop.time() - check.started), 0.0)
        check.waiters += 1
        try:
            done, _ = await asyncio.wait({check.task}, timeout=remaining)
        except asyncio.CancelledError:
            # The probe itself was cancelled: stop the check too, unless another probe still waits for it.
            check.waiters -= 1
            if not check.waiters:
                self._abandon(check)
            raise
        check.waiters -= 1
        if check.task in done and not check.task.cancelled():
            return check.task.result()
        self._abandon(check)
        return HealthStatus(
            status="DOWN",
            details={"database": dialect, "error": "TimeoutError", "message": f"no answer within {self._timeout:g} s"},
        )

    def _start(self, engine: Any, loop: asyncio.AbstractEventLoop) -> _Check:
        check = _Check(engine, loop.time())
        check.task = loop.create_task(_select_one(check))
        key = id(engine)
        self._checks[key] = check

        def _finished(task: asyncio.Task[HealthStatus]) -> None:
            if self._checks.get(key) is check:
                del self._checks[key]
            if not task.cancelled():
                task.exception()  # retrieved, so a late failure is not reported as never retrieved

        check.task.add_done_callback(_finished)
        return check

    @staticmethod
    def _abandon(check: _Check) -> None:
        """Stop a check that missed its deadline; it stays registered until its task has finished.

        The socket of the connection it holds is closed first, so the task does not wait in the
        driver's cleanup for a database that no longer answers on that connection. A check on a
        connection the pool shares with the application is only no longer waited for: cancelling it
        would make SQLAlchemy invalidate, and close, the connection the application is using (and
        lose a ``:memory:`` database with it). It ends by itself once the application's statement
        ahead of it has run.
        """
        if check.abandoned:
            return
        check.abandoned = True
        if _shares_connections(check.engine):
            return
        _terminate(check)
        check.task.cancel()


async def _select_one(check: _Check) -> HealthStatus:
    from sqlalchemy import literal, select

    engine = check.engine
    dialect = _dialect(engine)
    try:
        async with engine.connect() as conn:
            check.connection = conn.sync_connection.connection
            await conn.execute(select(literal(1)))
    except Exception as exc:
        return HealthStatus(
            status="DOWN",
            details={"database": dialect, "error": type(exc).__name__, "message": _masked(engine, exc)[:200]},
        )
    return HealthStatus(status="UP", details={"database": dialect})


def _terminate(check: _Check) -> None:
    """Close the socket of the connection *check* holds, without sending or awaiting anything.

    Called outside SQLAlchemy's greenlet, the async dialects' ``terminate`` takes the forced path
    (asyncpg ``Connection.terminate()``, aiosqlite ``stop()``, asyncmy/aiomysql ``close()``). The task
    then fails at once in the driver, and SQLAlchemy invalidates the pool entry. Nothing happens when
    the check holds no connection yet (it is still connecting; cancelling it is enough), when the
    connection is already back in the pool, or when the dialect cannot terminate.
    """
    connection = check.connection
    raw = connection.dbapi_connection if connection is not None else None
    if raw is None:
        return
    dialect = getattr(getattr(check.engine, "sync_engine", None), "dialect", None)
    if not getattr(dialect, "has_terminate", False):
        return
    try:
        dialect.do_terminate(raw)  # type: ignore[union-attr]
    except Exception:
        _logger.debug("db_health_terminate_failed", exc_info=True)


def _shares_connections(engine: Any) -> bool:
    """Whether the pool hands the same connection to every checkout (SQLite ``:memory:``)."""
    from sqlalchemy.pool import SingletonThreadPool, StaticPool

    pool = getattr(getattr(engine, "sync_engine", None), "pool", None)
    return isinstance(pool, StaticPool | SingletonThreadPool)


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
