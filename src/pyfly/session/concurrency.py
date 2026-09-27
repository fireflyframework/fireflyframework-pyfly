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
"""Session concurrency control — Spring Security's ``maximumSessions``.

Limits the number of concurrent sessions per authenticated principal. When the cap is
exceeded a new login is either rejected (``reject-new``) or the oldest session is evicted
(``evict-oldest``). Enforced at the single point where a principal becomes bound to a
session (OAuth2 login). With no cap configured the registry is unused and behavior is
unchanged.

- **Only live sessions count.** Given the session store, the controller drops, at each login, the
  registrations of the principal's sessions the store no longer has (expired, invalidated, lost in a
  restart), so a user whose sessions ended without a logout is never locked out; a purge drops the others
  (from an :class:`ExpiringSessionRegistry`: the SQL and in-memory ones).
  The store must be as shared as the registry (the auto-configuration gives no process-local store to a
  cross-process registry).
- **The cap holds under concurrency.** Counting the principal's sessions, evicting and registering the new
  one is one atomic step of an :class:`AtomicSessionRegistry` (one unit of work that holds the principal's
  row on SQL, one script on Redis, one lock in memory), so concurrent logins, on one instance or several,
  never exceed it. A registry with only the :class:`SessionRegistry` operations is serialized per principal
  within this process.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pyfly.session.ports.outbound import SessionStore

logger = logging.getLogger(__name__)


@runtime_checkable
class SessionRegistry(Protocol):
    """Per-principal index of live session ids (kept separate from the SessionStore)."""

    async def register(self, principal: str, session_id: str, created_at: float) -> None: ...

    async def deregister(self, principal: str, session_id: str) -> None: ...

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        """``(session_id, created_at)`` for *principal*, oldest first."""
        ...

    async def count(self, principal: str) -> int: ...


@dataclass(frozen=True)
class SessionRegistration:
    """What :meth:`AtomicSessionRegistry.register_limited` did: whether the session was registered, and the
    sessions of the principal it deregistered to make room (oldest first)."""

    accepted: bool
    evicted: tuple[str, ...] = ()


@runtime_checkable
class AtomicSessionRegistry(Protocol):
    """A registry that checks the cap and registers in one atomic step, across every process sharing it."""

    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration:
        """Register *session_id* for *principal* if the principal's other sessions leave room under
        *max_sessions*; otherwise, with *evict_oldest*, deregister the oldest until it fits and register it,
        or else register nothing. Nothing another login does interleaves."""
        ...


@runtime_checkable
class ExpiringSessionRegistry(Protocol):
    """A registry whose registrations come due for a liveness check (the SQL and in-memory registries): the
    purge drops the registrations of sessions the store no longer has and renews the others."""

    async def expired_sessions(self, *, limit: int) -> list[tuple[str, str]]:
        """Up to *limit* ``(principal, session_id)`` registrations due for a check, most overdue first."""
        ...

    async def renew(self, session_ids: Sequence[str]) -> None:
        """Push the next check of *session_ids* one registration lifetime away."""
        ...


def plan_registration(
    existing: Sequence[tuple[str, float]], *, max_sessions: int, evict_oldest: bool
) -> tuple[bool, list[str]]:
    """Whether a new session is admitted beside the principal's *existing* sessions (``(session_id,
    created_at)``, oldest first, the new session excluded), and which of them it evicts."""
    if len(existing) + 1 <= max_sessions:
        return True, []
    if not evict_oldest:
        return False, []
    return True, [session_id for session_id, _created in existing[: len(existing) + 1 - max(max_sessions, 0)]]


class InMemorySessionRegistry:
    """In-process :class:`SessionRegistry` (mirrors InMemorySessionStore); its capped registration is atomic
    within the process.

    It is an :class:`ExpiringSessionRegistry`: a registration comes due for a liveness check one *ttl* after
    it was registered or renewed, so the controller's purge drops the registrations of sessions that ended
    without a logout, whatever the cap (with max-sessions -1 nothing else would).

    Args:
        ttl: How long a registration goes before its liveness is checked again (the session timeout, by
            default ``pyfly.session.ttl``); seconds or a ``timedelta``, positive (``ValueError`` otherwise).
        clock: The current UTC instant (tests pass their own).
    """

    def __init__(
        self, *, ttl: timedelta | float = timedelta(seconds=1800), clock: Callable[[], datetime] | None = None
    ) -> None:
        self._ttl = ttl if isinstance(ttl, timedelta) else timedelta(seconds=float(ttl))
        if self._ttl <= timedelta(0):
            # A renewal would leave the registration due: the purge would check the same batch forever.
            raise ValueError(f"The session-registry ttl must be positive, got {self._ttl}")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._by_principal: dict[str, dict[str, float]] = {}
        # Session id -> (its next liveness check, its principal): a session id has one principal, as a SQL
        # registration row does.
        self._due: dict[str, tuple[datetime, str]] = {}
        self._lock = asyncio.Lock()

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        async with self._lock:
            self._add(principal, session_id, created_at)

    async def deregister(self, principal: str, session_id: str) -> None:
        async with self._lock:
            self._remove(principal, session_id)

    def _add(self, principal: str, session_id: str, created_at: float) -> None:
        registered = self._due.get(session_id)
        if registered is not None and registered[1] != principal:
            self._remove(registered[1], session_id)
        self._by_principal.setdefault(principal, {})[session_id] = created_at
        self._due[session_id] = (self._clock() + self._ttl, principal)

    def _remove(self, principal: str, session_id: str) -> None:
        sessions = self._by_principal.get(principal)
        if sessions is not None:
            sessions.pop(session_id, None)
            if not sessions:
                del self._by_principal[principal]
        registered = self._due.get(session_id)
        if registered is not None and registered[1] == principal:
            del self._due[session_id]

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        async with self._lock:
            sessions = self._by_principal.get(principal, {})
            return sorted(sessions.items(), key=lambda kv: kv[1])

    async def count(self, principal: str) -> int:
        async with self._lock:
            return len(self._by_principal.get(principal, {}))

    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration:
        async with self._lock:
            sessions = self._by_principal.get(principal, {})
            existing = sorted(((sid, at) for sid, at in sessions.items() if sid != session_id), key=lambda kv: kv[1])
            accepted, evicted = plan_registration(existing, max_sessions=max_sessions, evict_oldest=evict_oldest)
            if not accepted:
                return SessionRegistration(False)
            for evicted_id in evicted:
                self._remove(principal, evicted_id)
            self._add(principal, session_id, created_at)
            return SessionRegistration(True, tuple(evicted))

    async def expired_sessions(self, *, limit: int) -> list[tuple[str, str]]:
        """Up to *limit* ``(principal, session_id)`` registrations due for a liveness check, most overdue
        first."""
        now = self._clock()
        async with self._lock:
            due = heapq.nsmallest(
                limit, ((at, session_id, principal) for session_id, (at, principal) in self._due.items() if at <= now)
            )
        return [(principal, session_id) for _at, session_id, principal in due]

    async def renew(self, session_ids: Sequence[str]) -> None:
        """Push the next liveness check of *session_ids* one *ttl* away."""
        next_check = self._clock() + self._ttl
        async with self._lock:
            for session_id in session_ids:
                registered = self._due.get(session_id)
                if registered is not None:
                    self._due[session_id] = (next_check, registered[1])


@dataclass(frozen=True)
class ConcurrencyControlPolicy:
    """Concurrency cap configuration."""

    max_sessions: int = -1  # -1 = unlimited (default; behavior unchanged)
    strategy: str = "evict-oldest"  # "evict-oldest" | "reject-new"


class SessionConcurrencyController:
    """Enforces a per-principal session cap on login and cleans up on logout.

    Args:
        registry: The per-principal session index.
        policy: The cap and the strategy.
        session_deleter: Deletes an evicted session from the session store (by default *session_store*'s
            ``delete``).
        session_store: The session store: a session counts toward the cap while the store has it (see the
            module documentation). It must hold every session the registry counts: beside a registry shared
            by several instances, a store each instance keeps to itself takes the others' live sessions for
            dead ones. Without it every registered session counts until it logs out or is evicted.
        purge_interval: How often a login also purges the registrations of dead sessions from a registry
            that supports it, :attr:`LOGIN_PURGE_BATCH` of them at a time (``None``: only :meth:`purge_expired`
            does).

    A session must be in the store before its login registers it (the OAuth2 login handler saves it first),
    or a concurrent login of the same principal takes it for a dead one.
    """

    #: How many registrations one step of :meth:`purge_expired` checks.
    PURGE_BATCH = 500
    #: How many registrations a login's own purge checks (a backlog goes to the next logins).
    LOGIN_PURGE_BATCH = 50
    #: How long :meth:`stop` waits for the deletions of evicted sessions still in flight, in seconds.
    EVICTION_STOP_TIMEOUT = 30.0

    def __init__(
        self,
        registry: SessionRegistry,
        policy: ConcurrencyControlPolicy,
        *,
        session_deleter: Callable[[str], Awaitable[None]] | None = None,
        session_store: SessionStore | None = None,
        purge_interval: timedelta | None = timedelta(seconds=60),
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._store = session_store
        # Evicts the session-store entry of an evicted session.
        self._delete = session_deleter if session_deleter is not None else getattr(session_store, "delete", None)
        self._purge_interval = purge_interval.total_seconds() if purge_interval is not None else None
        self._last_purge = time.monotonic()
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._evictions: set[asyncio.Task[None]] = set()

    @property
    def registry(self) -> SessionRegistry:
        """The per-principal session index."""
        return self._registry

    @property
    def session_store(self) -> SessionStore | None:
        """The session store that tells live sessions from dead ones, if any."""
        return self._store

    async def start(self) -> None:
        """Start the registry (a SQL registry creates or checks its tables): a lifecycle bean's start."""
        start = getattr(self._registry, "start", None)
        if callable(start):
            await start()

    async def stop(self) -> None:
        """Wait for the evicted sessions still being deleted, up to :attr:`EVICTION_STOP_TIMEOUT` seconds (a
        store that stopped answering must not hold the shutdown; what is left is logged as
        ``session_eviction_unfinished``), then stop the registry (idempotent)."""
        if self._evictions:
            _done, pending = await asyncio.wait(set(self._evictions), timeout=self.EVICTION_STOP_TIMEOUT)
            if pending:
                logger.warning("session_eviction_unfinished", extra={"pending": len(pending)})
        stop = getattr(self._registry, "stop", None)
        if callable(stop):
            await stop()

    async def on_login(self, principal: str, session_id: str, created_at: float) -> bool:
        """Register the new session, enforcing the cap. Returns ``False`` if rejected."""
        if self._policy.max_sessions < 0:
            await self._registry.register(principal, session_id, created_at)
            await self._purge_if_due()
            return True

        await self._drop_dead_sessions(principal, session_id)
        evict_oldest = self._policy.strategy != "reject-new"
        registry = self._registry
        if isinstance(registry, AtomicSessionRegistry):
            result = await registry.register_limited(
                principal, session_id, created_at, max_sessions=self._policy.max_sessions, evict_oldest=evict_oldest
            )
        else:
            result = await self._register_limited(principal, session_id, created_at, evict_oldest=evict_oldest)

        if not result.accepted:
            logger.info(
                "Rejected login for %r: max concurrent sessions (%d) reached", principal, self._policy.max_sessions
            )
            return False
        if result.evicted and self._delete is not None:
            await self._delete_all_evicted(principal, result.evicted)
        await self._purge_if_due()
        return True

    async def on_logout(self, principal: str, session_id: str) -> None:
        await self._registry.deregister(principal, session_id)

    async def purge_expired(self) -> int:
        """Drop the registrations due for a check whose sessions the store no longer has, and renew the
        others; return how many were dropped. Needs the session store and an :class:`ExpiringSessionRegistry`
        (otherwise it does nothing)."""
        purged = 0
        while True:
            dropped, checked = await self._purge_batch(self.PURGE_BATCH)
            purged += dropped
            if checked < self.PURGE_BATCH:
                return purged

    # -- internals ------------------------------------------------------------------------------------------

    async def _drop_dead_sessions(self, principal: str, session_id: str) -> None:
        """Deregister the principal's sessions the store no longer has (as Spring's
        ``getAllSessions(principal, false)`` leaves the expired ones out)."""
        store = self._store
        if store is None:
            return
        for registered, _created in await self._registry.list_sessions(principal):
            if registered != session_id and not await store.exists(registered):
                await self._registry.deregister(principal, registered)

    async def _register_limited(
        self, principal: str, session_id: str, created_at: float, *, evict_oldest: bool
    ) -> SessionRegistration:
        """The capped registration on a registry without an atomic one: serialized per principal within this
        process (other processes can still interleave)."""
        lock = self._locks.get(principal)
        if lock is None:
            lock = self._locks[principal] = asyncio.Lock()
        async with lock:
            existing = [entry for entry in await self._registry.list_sessions(principal) if entry[0] != session_id]
            accepted, evicted = plan_registration(
                existing, max_sessions=self._policy.max_sessions, evict_oldest=evict_oldest
            )
            if not accepted:
                return SessionRegistration(False)
            for evicted_id in evicted:
                await self._registry.deregister(principal, evicted_id)
            await self._registry.register(principal, session_id, created_at)
            return SessionRegistration(True, tuple(evicted))

    async def _delete_all_evicted(self, principal: str, evicted: Sequence[str]) -> None:
        """Delete the evicted sessions from the store, shielded from the login's cancellation: the registry
        committed their eviction already, so a deletion abandoned halfway would leave a session usable and no
        longer counted. The deletion runs outside the caller's unit of work, as the registry's eviction did,
        and :meth:`stop` waits for the deletions still in flight."""
        from pyfly.data.transaction import detached

        task = detached(self._delete_evicted(principal, evicted), name="session-eviction")
        self._evictions.add(task)
        task.add_done_callback(self._evictions.discard)
        await asyncio.shield(task)

    async def _delete_evicted(self, principal: str, evicted: Sequence[str]) -> None:
        """Delete each evicted session from the store. The registry no longer holds them, so a failure is
        logged (``session_eviction_failed``) and the login goes on: that session stays usable until it expires
        or is invalidated, beside the sessions the cap counts."""
        delete = self._delete
        if delete is None:
            return
        for session_id in evicted:
            try:
                await delete(session_id)
            except Exception:  # noqa: BLE001 — the login was admitted; a stray session outlives it at worst
                logger.warning("session_eviction_failed", extra={"principal": principal}, exc_info=True)

    async def _purge_batch(self, limit: int) -> tuple[int, int]:
        registry = self._registry
        store = self._store
        if store is None or not isinstance(registry, ExpiringSessionRegistry):
            return 0, 0
        due = await registry.expired_sessions(limit=limit)
        live: list[str] = []
        dropped = 0
        for principal, session_id in due:
            if await store.exists(session_id):
                live.append(session_id)
            else:
                await registry.deregister(principal, session_id)
                dropped += 1
        if live:
            await registry.renew(live)
        return dropped, len(due)

    async def _purge_if_due(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        try:
            dropped, checked = await self._purge_batch(self.LOGIN_PURGE_BATCH)
        except Exception:  # noqa: BLE001 — a failed purge must never fail a login
            logger.warning("session_registry_purge_failed", exc_info=True)
            return
        if checked >= self.LOGIN_PURGE_BATCH:
            self._last_purge = float("-inf")  # a backlog: the next login checks the next batch
        if dropped:
            logger.debug("session_registrations_purged", extra={"count": dropped})
