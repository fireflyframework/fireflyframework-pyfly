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
  keep the probe waiting in the driver's cleanup (asyncpg sends a cancel request over a new connection,
  then waits, with no timeout, for the server's answer on the silent one);
- the late check does not keep its connection. The socket of the connection it holds, or is still
  checking out (reconnecting, recycling or pre-pinging it), is closed on the spot (the dialect's
  ``terminate``, which sends nothing and waits for nothing), and then the check is cancelled. A
  middlebox that drops the packets of flows it has expired, without a reset (an Azure Load Balancer
  without TCP reset on idle, its default, or a stateful firewall that drops packets of flows it no
  longer tracks), black-holes those pooled connections while the database accepts new ones. (One that
  answers them with a reset, such as an AWS NAT gateway or an Azure NAT Gateway, makes the connection
  fail at once instead: with pre-ping on the check reconnects, and without it that probe answers DOWN
  with the disconnect error.) Cancelled on a black-holed connection without this, asyncpg sends a
  cancel request and waits for the server's answer on the dead socket with no timeout, even once the
  connection is lost: the check would never end and would keep its pool slot. With it, the check
  usually ends at once and gives its slot back. When the driver turns the cancellation into a
  disconnect error instead (a pre-ping whose rollback fails on the closed socket), SQLAlchemy
  reconnects, within the connect timeout, and the check runs its ``SELECT 1`` on the new connection.
  Each black-holed pooled connection costs one probe, pre-ping on or off: that probe answers DOWN and
  the next one runs on another connection. When a middlebox silently forgets every idle flow at once,
  up to one probe per idle connection answers DOWN, so with the readiness probe's default
  ``failureThreshold`` of 3 a pool holding three or more idle connections can take the replica out of
  rotation until a probe answers UP again. A check still in its checkout is reached through the pool
  entry that the registry's pool
  (:class:`~pyfly.data.relational.datasource_registry.MeteredAsyncQueuePool`) reports; for an engine
  the registry did not build, see the next point. A check on a connection the pool shares with the
  application (``StaticPool`` for SQLite ``:memory:``) is neither closed nor cancelled, only no longer
  waited for: it runs after the application's statement ahead of it;
- while a check that missed its deadline is still winding down with its connection, the next probe of
  that datasource answers DOWN at once instead of borrowing another connection. A late check that never
  got its connection (stuck connecting or pre-pinging) does not hold the next probes back: they start a
  new check on another connection while fewer than two late checks of that datasource are still
  running, and answer DOWN (``previous check still running``) beyond that. This bounds what an engine
  the registry did not build (a user-supplied ``async_engine`` bean) with pool pre-ping on can lose:
  its pool reports no entry, so a check stuck in an asyncpg pre-ping cannot be closed and, once
  cancelled, never ends, not even when the kernel gives up on the socket. Each silent drop can leave
  one such check behind, holding a pool slot, two per engine at most; after that the datasource answers
  ``previous check still running`` until the application stops, and a pool of two connections or fewer
  without overflow is starved;
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
    from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

_logger = logging.getLogger(__name__)

_MAX_LATE_CHECKS = 2
"""Late checks of one engine, still running, beyond which a probe starts no new check."""


class _Check:
    """One ``SELECT 1`` in flight on an engine, shared by the probes that arrive while it runs."""

    __slots__ = ("abandoned", "connection", "engine", "entry", "started", "task", "waiters")

    task: asyncio.Task[HealthStatus]

    def __init__(self, engine: Any, started: float) -> None:
        self.engine = engine
        self.started = started
        self.abandoned = False
        # The pooled connection the check holds, once it has one. Its ``dbapi_connection`` turns None
        # when the connection goes back to the pool, so a late terminate never hits a connection that
        # someone else may have borrowed since.
        self.connection: PoolProxiedConnection | None = None
        # The pool entry the check is checking out, until it holds the connection: the registry's pool
        # reports it before the pre-ping. An entry keeps its ``dbapi_connection`` in the pool, so it is
        # dropped the moment the checkout ends, before the check can yield to another task.
        self.entry: ConnectionPoolEntry | None = None
        # Probes waiting for this check; a cancelled probe stops the check only when it was the last.
        self.waiters = 0

    def checking_out(self, entry: ConnectionPoolEntry) -> None:
        """Remember the pool entry of the check's own checkout, which the registry's pool reports.

        The pool reports only the checkouts the check's task makes on the engine's pool, and only the
        first counts: a checkout made by another task, or nested in the check's own (a pool listener
        borrowing a connection), holds an entry the check must never close.
        """
        if self.connection is None and self.entry is None and asyncio.current_task() is self.task:
            self.entry = entry


class SqlAlchemyHealthIndicator:
    """Database health probe — ``UP`` iff ``SELECT 1`` succeeds within the timeout on every datasource."""

    probe_groups: ClassVar[frozenset[ProbeGroup]] = frozenset({ProbeGroup.READINESS})

    def __init__(self, engine: Any, *, registry: Any = None, timeout: float = 2.0) -> None:
        self._engine = engine
        self._registry = registry
        self._timeout = timeout
        # The check in flight per engine (keyed by identity); an entry leaves when its task finishes.
        self._checks: dict[int, _Check] = {}
        # The checks that missed their deadline and are still running, per engine.
        self._late: dict[int, set[_Check]] = {}

    async def health(self) -> HealthStatus:
        dialect = _dialect(self._engine)
        if self._registry is not None and getattr(self._registry, "closed", False):
            # The context stopped (or is stopping): its engines are disposed and refuse to connect.
            return HealthStatus(status="OUT_OF_SERVICE", details={"database": dialect, "reason": "datasources closed"})
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
            if check.connection is not None or self._late_checks(engine, loop) >= _MAX_LATE_CHECKS:
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
            # It is stuck before it got its connection (a pre-ping on a pool that reports no entry): a
            # new check borrows another connection rather than answer DOWN until the stuck one ends.
            check = None
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
            late = self._late.get(key)
            if late is not None:
                late.discard(check)
                if not late:
                    del self._late[key]
            if not task.cancelled():
                task.exception()  # retrieved, so a late failure is not reported as never retrieved

        check.task.add_done_callback(_finished)
        return check

    def _late_checks(self, engine: Any, loop: asyncio.AbstractEventLoop) -> int:
        """How many checks of *engine* that missed their deadline are still running on *loop*."""
        key = id(engine)
        late = self._late.get(key)
        if late is None:
            return 0
        late -= {check for check in late if check.task.get_loop() is not loop}  # left by a loop that is gone
        if not late:
            del self._late[key]
        return len(late)

    def _abandon(self, check: _Check) -> None:
        """Stop a check that missed its deadline; it stays registered until its task has finished.

        The socket of the connection it holds, or is still checking out, is closed first, so the task
        does not wait in the driver's cleanup for a database that no longer answers on that connection.
        A check on a connection the pool shares with the application is only no longer waited for:
        cancelling it would make SQLAlchemy invalidate, and close, the connection the application is
        using (and lose a ``:memory:`` database with it). It ends by itself once the application's
        statement ahead of it has run.
        """
        if check.abandoned:
            return
        check.abandoned = True
        if not check.task.done():
            self._late.setdefault(id(check.engine), set()).add(check)
        if _shares_connections(check.engine):
            return
        _terminate(check)
        check.task.cancel()


async def _select_one(check: _Check) -> HealthStatus:
    from sqlalchemy import literal, select

    from pyfly.data.relational.datasource_registry import observing_checkouts

    engine = check.engine
    dialect = _dialect(engine)
    try:
        with observing_checkouts(check.checking_out, pool=getattr(getattr(engine, "sync_engine", None), "pool", None)):
            async with engine.connect() as conn:
                check.connection = conn.sync_connection.connection
                check.entry = None
                await conn.execute(select(literal(1)))
    except Exception as exc:
        return HealthStatus(
            status="DOWN",
            details={"database": dialect, "error": type(exc).__name__, "message": _masked(engine, exc)[:200]},
        )
    finally:
        check.entry = None  # the checkout is over (or failed): the entry may serve another checkout now
    return HealthStatus(status="UP", details={"database": dialect})


def _terminate(check: _Check) -> None:
    """Close the socket of the connection *check* holds or is checking out, sending and awaiting nothing.

    Called outside SQLAlchemy's greenlet, the async dialects' ``terminate`` takes the forced path
    (asyncpg ``Connection.terminate()``, aiosqlite ``stop()``, asyncmy/aiomysql ``close()``). The task
    then fails at once in the driver, and SQLAlchemy invalidates the pool entry. A check still in its
    checkout (pre-pinging, recycling or reconnecting) is reached through the pool entry it reported:
    cancelled, asyncpg's pre-ping would wait on a black-holed socket for good. Nothing happens when the
    check has no connection yet (it is still connecting, and cancelling it is enough), when its pool
    reports no entry, when the connection is already back in the pool, or when the dialect cannot
    terminate.
    """
    if check.connection is not None:
        raw = check.connection.dbapi_connection  # None once the connection is back in the pool
    elif check.entry is not None:
        raw = check.entry.dbapi_connection  # None while the checkout (re)connects
    else:
        raw = None
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
