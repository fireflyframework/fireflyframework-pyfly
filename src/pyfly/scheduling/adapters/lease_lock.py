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
"""A portable lease table (ShedLock-style): the default database lock for ``@scheduled`` jobs.

A named lock is a row of the framework table ``pyfly_locks``
(:func:`~pyfly.data.relational.framework_schema.locks_table`): who holds it (``locked_by``), until when
(``lock_until``), and a fencing token (``fence``) that grows at every acquisition. Taking the lock is one
statement on PostgreSQL and SQLite, ``INSERT ... ON CONFLICT (name) DO UPDATE ... WHERE lock_until <= now``
(it inserts the row, takes an ended lease over, or leaves a live one alone); on other backends a conditional
``UPDATE ... WHERE lock_until <= now``, after an ``INSERT`` of the row when this instance has not found it
yet. Releasing it sets ``lock_until`` to now. Each statement runs in a short unit of its own, committed at
once (on PostgreSQL an autocommit statement, one round trip), and never part of the caller's transaction.

Compared with holding a session-level advisory lock for the whole job:

- **No connection is held while the job runs.** A server's ``idle_in_transaction_session_timeout`` cannot
  drop the lock mid-job, and the job's own units of work have the whole pool.
- **The TTL is honored on every backend.** A lease ends at ``lock_until``: a hung job blocks its schedule for
  at most ``lock_ttl``, after which another node runs it. The hung job, when it ends, does not release a lease
  another node took since (each acquisition has a ``locked_by`` of its own), and its release logs a WARNING.
- **It works on every backend SQLAlchemy supports**, with the dialect's own statements
  (:mod:`pyfly.data.relational.upsert`).

The nodes compare ``lock_until`` against their own clocks, as ShedLock does: keep them synchronized (NTP) to
well within the TTLs. Besides the :class:`~pyfly.scheduling.lock.DistributedLock` port
(:meth:`LeaseLock.try_acquire`, :meth:`LeaseLock.release`), leases can be taken with a wait
(:meth:`LeaseLock.acquire`), renewed (:meth:`LeaseLock.extend`) and inspected (:meth:`LeaseLock.holder`),
for work that must run on one node at a time (a projection, a schema migration).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import UnitOfWork, auto_unit, resolve_manager, untracked

if TYPE_CHECKING:
    from sqlalchemy import Table

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Lease:
    """A lease as the table holds it: ``locked_by`` identifies the acquisition that took it (``owner``, the
    holding :class:`LeaseLock`, then a token of its own), ``fence`` grows by one at every acquisition."""

    name: str
    owner: str
    locked_by: str
    until: datetime
    fence: int


def default_owner() -> str:
    """``host:pid:random``: tells the nodes, and two lock instances of one process, apart (the host name is
    cut to 200 characters, so the owner fits :attr:`LeaseLock.MAX_OWNER_LENGTH`)."""
    return f"{socket.gethostname()[:200]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseLock:
    """A :class:`~pyfly.scheduling.lock.DistributedLock` on a lease table (see the module documentation).

    *datasource* is where the table lives: an ``AsyncEngine``, a registry ``DataSource`` or a datasource
    name. *owner* identifies this instance in ``locked_by`` (by default ``host:pid:random``, at most
    :attr:`MAX_OWNER_LENGTH` characters). With *create_table* false the table is only checked at
    :meth:`start`. *clock* gives the current UTC instant.

    A lease name is at most :attr:`MAX_NAME_LENGTH` characters: a longer one raises ``ValueError`` before
    any statement runs (MySQL and MariaDB's ``INSERT IGNORE`` would truncate it, and the truncated lease
    could never be released or taken over again).
    """

    #: The longest lease name: the length of ``pyfly_locks.name``.
    MAX_NAME_LENGTH = 255
    #: The longest *owner*: a ``locked_by`` is the owner, ``/`` and a token of 8 characters, in 255.
    MAX_OWNER_LENGTH = MAX_NAME_LENGTH - 9

    def __init__(
        self,
        datasource: Any,
        *,
        table_name: str = "pyfly_locks",
        owner: str | None = None,
        create_table: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = datasource
        self._table_name = table_name
        self._owner = owner or default_owner()
        if len(self._owner) > self.MAX_OWNER_LENGTH:
            raise ValueError(
                f"A lease owner is at most {self.MAX_OWNER_LENGTH} characters (each acquisition's locked_by adds "
                f"a token of 9 to it, in {self.MAX_NAME_LENGTH}): {self._owner[:40]!r}..."
            )
        self._create_table = create_table
        self._clock = clock or (lambda: datetime.now(UTC))
        self._table_object: Table | None = None
        self._started = False
        # Whether an acquisition is one statement (PostgreSQL, SQLite), resolved at the first one; elsewhere
        # the lock rows this instance found: taking them is one UPDATE, without the INSERT that creates them.
        self._one_statement: bool | None = None
        self._known: set[str] = set()
        # name -> the acquisitions this instance holds, innermost last: (task, locked_by)
        self._held: dict[str, list[tuple[asyncio.Task[Any] | None, str]]] = {}

    @property
    def owner(self) -> str:
        """This instance's identity; each acquisition's ``locked_by`` starts with it."""
        return self._owner

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import locks_table

            self._table_object = locks_table(self._table_name)
        return self._table_object

    @contextlib.asynccontextmanager
    async def _unit(self, *, read_only: bool = False) -> AsyncIterator[UnitOfWork]:
        # A unit of its own, committed at once: a lease is never part of the caller's transaction. Nor is it the
        # caller's work to track (untracked): a step that took a lease on its way has committed nothing of its own.
        with untracked():
            async with auto_unit(resolve_manager(self._target), read_only=read_only, autocommit=True) as unit:
                yield unit

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the lease table when allowed, and check it (the first acquisition does it when nothing
        started the lock)."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, self._table, create=self._create_table)
        self._started = True

    async def stop(self) -> None:
        """Release the leases this instance still holds (the jobs have drained by now). Idempotent."""
        for name in list(self._held):
            while self._held.get(name):
                try:
                    await self.release(name)
                except Exception:  # noqa: BLE001 — stopping goes on; the lease ends at its ttl anyway
                    _logger.warning("scheduler_lease_release_failed", extra={"lock": name}, exc_info=True)
                    self._held.pop(name, None)

    # ------------------------------------------------------------------
    # DistributedLock
    # ------------------------------------------------------------------

    async def try_acquire(self, name: str, ttl: float) -> bool:
        """Take lease *name* for *ttl* seconds, unless another acquisition holds it now."""
        return await self._take(name, ttl) is not None

    async def release(self, name: str) -> None:
        """End this instance's lease *name* now (no-op when it holds none).

        A lease that already ended (the job ran past its ttl) is left alone, whoever holds it since, and a
        WARNING says so."""
        locked_by = self._pop(name)
        if locked_by is None:
            return
        from sqlalchemy import update

        table = self._table
        now = self._clock()
        statement = (
            update(table)
            .where(table.c.name == name, table.c.locked_by == locked_by, table.c.lock_until > now)
            .values(lock_until=now)
        )
        async with self._unit() as unit:
            result = await unit.resource.execute(statement)
        if int(result.rowcount) == 0:
            _logger.warning(
                "scheduler_lease_expired_before_release",
                extra={
                    "lock": name,
                    "locked_by": locked_by,
                    "hint": "the job ran longer than its lock ttl, so another node may have run it meanwhile; "
                    "raise lock_ttl above the job's longest run",
                },
            )

    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------

    async def acquire(self, name: str, ttl: float, *, wait: float = 0.0, poll_interval: float = 0.25) -> Lease | None:
        """Take lease *name* for *ttl* seconds, trying again every *poll_interval* seconds for up to *wait*
        seconds; return the lease (with its fencing token), or ``None`` when another holder kept it.

        An acquisition whose lease cannot be read back (it ended already, the ttl being shorter than the round
        trip, or the read failed) is not kept: the lease ends at its ttl."""
        deadline = time.monotonic() + wait
        while True:
            locked_by = await self._take(name, ttl)
            if locked_by is not None:
                try:
                    lease = await self.holder(name)
                except BaseException:
                    self._forget(name, locked_by)
                    raise
                if lease is not None and lease.locked_by == locked_by:
                    return lease
                self._forget(name, locked_by)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))

    async def extend(self, name: str, ttl: float) -> bool:
        """Move this instance's live lease *name* to end *ttl* seconds from now; ``False`` when it holds none
        (it ended, or another node took it)."""
        entries = self._held.get(name)
        if not entries:
            return False
        from sqlalchemy import update

        locked_by = self._entry(entries)[1]
        table = self._table
        now = self._clock()
        statement = (
            update(table)
            .where(table.c.name == name, table.c.locked_by == locked_by, table.c.lock_until > now)
            .values(lock_until=now + timedelta(seconds=ttl))
        )
        async with self._unit() as unit:
            result = await unit.resource.execute(statement)
        return int(result.rowcount) == 1

    async def holder(self, name: str) -> Lease | None:
        """The live lease *name* (whoever holds it), or ``None`` when it is free."""
        from sqlalchemy import select

        table = self._table
        statement = select(table.c.locked_by, table.c.lock_until, table.c.fence).where(
            table.c.name == name, table.c.lock_until > self._clock()
        )
        async with self._unit(read_only=True) as unit:
            row = (await unit.resource.execute(statement)).first()
        if row is None:
            return None
        locked_by = str(row.locked_by)
        return Lease(
            name=name,
            owner=locked_by.rpartition("/")[0] or locked_by,
            locked_by=locked_by,
            until=row.lock_until,
            fence=int(row.fence),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _take(self, name: str, ttl: float) -> str | None:
        """One acquisition attempt; returns its ``locked_by`` when it took the lease."""
        from pyfly.data.relational.framework_schema import framework_engine
        from pyfly.data.relational.upsert import backend_name, insert_if_absent, native_conditional_insert, take_over

        self._check_name(name)
        if not self._started:
            await self.start()
        if self._one_statement is None:
            self._one_statement = native_conditional_insert(backend_name(framework_engine(self._target)))

        table = self._table
        now = self._clock()
        locked_by = f"{self._owner}/{uuid.uuid4().hex[:8]}"
        values = {"name": name, "lock_until": now + timedelta(seconds=ttl), "locked_at": now, "locked_by": locked_by}
        inserted = {**values, "fence": 1}
        taken_over = {"fence": table.c.fence + 1}
        ended = table.c.lock_until <= now
        taken = False
        if self._one_statement:
            # PostgreSQL and SQLite: insert the row, take an ended lease over, or leave a live one alone.
            async with self._unit() as unit:
                taken = await insert_if_absent(
                    unit.resource, table, inserted, key=["name"], replace_where=ended, replace_with=taken_over
                )
        else:
            known = name in self._known
            if not known:
                async with self._unit() as unit:
                    taken = await insert_if_absent(unit.resource, table, inserted, key=["name"])
            if not taken:
                async with self._unit() as unit:
                    taken = await take_over(unit.resource, table, {**values, **taken_over}, key=["name"], where=ended)
            if taken or not known:
                self._known.add(name)  # the row is there: the next attempt is one UPDATE
            else:
                # Held elsewhere, or its row is gone (deleted by hand): the next attempt inserts it first again.
                self._known.discard(name)
        if not taken:
            return None
        self._held.setdefault(name, []).append((asyncio.current_task(), locked_by))
        return locked_by

    def _check_name(self, name: str) -> None:
        if len(name) > self.MAX_NAME_LENGTH:
            raise ValueError(f"A lease name is at most {self.MAX_NAME_LENGTH} characters: {name[:40]!r}...")

    @staticmethod
    def _entry(entries: list[tuple[asyncio.Task[Any] | None, str]]) -> tuple[asyncio.Task[Any] | None, str]:
        """The acquisition of the running task, else the latest (a lease taken and released by one task)."""
        task = asyncio.current_task()
        for entry in reversed(entries):
            if entry[0] is task:
                return entry
        return entries[-1]

    def _forget(self, name: str, locked_by: str) -> None:
        entries = self._held.get(name, [])
        for entry in entries:
            if entry[1] == locked_by:
                entries.remove(entry)
                break
        if not entries:
            self._held.pop(name, None)

    def _pop(self, name: str) -> str | None:
        entries = self._held.get(name)
        if not entries:
            return None
        entry = self._entry(entries)
        entries.remove(entry)
        if not entries:
            del self._held[name]
        return entry[1]
