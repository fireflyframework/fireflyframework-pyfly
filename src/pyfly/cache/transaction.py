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
"""A cache whose writes wait for the unit of work to commit (Spring's ``TransactionAwareCacheDecorator``).

Inside a unit of work (``@transactional``, a ``TransactionTemplate`` block, a repository call's auto
unit), :class:`TransactionAwareCache` registers ``put``, ``evict``, ``evict_by_prefix`` and ``clear`` as
after-commit synchronizations (:func:`pyfly.data.transaction.after_commit`):

- they run once the unit has committed, so no other request is served a value that was never committed,
  and no concurrent reader re-caches the old value after an eviction that ran before the commit;
- they are dropped when the unit rolls back (or its commit fails), which leaves the cache exactly as it
  was, consistent with the database.

The unit's own reads do not see them before the commit. They belong to the unit, not to a savepoint: a
write registered inside a ``Propagation.NESTED`` scope that rolls back to its savepoint still runs when the
unit commits (evict the key where the failed step is handled; that eviction runs after it).

Outside a unit they run at once. Reads (``get``, ``exists``) always run at once, and so do
``put_if_absent`` and the explicitly immediate :meth:`TransactionAwareCache.evict_if_present` and
:meth:`TransactionAwareCache.invalidate`.

Whatever runs at once runs outside the caller's unit (:func:`pyfly.data.transaction.outside_transaction`),
in the calling task: a cache on a database that joins the unit bound for its datasource (a framework adapter
running through ``infrastructure_unit()``) then gives each statement a short unit of its own. The caller's
rollback does not undo an immediate write, and no other request waits for the row locks of the caller's unit
or deadlocks with it on the cache's rows. The cost: during a business unit, each cache statement on a
database-backed cache checks out one more pooled connection of the cache's datasource. On SQLite, whose
database has one writer, an immediate write to a cache on the database of the caller's write unit is refused
at once (``IllegalTransactionStateError``; logged with ``on_write_error="log"``) instead of waiting
``busy_timeout`` for the lock the caller holds; reads still run.

A deferred ``put`` stores a copy of the value taken when it was registered
(:func:`~pyfly.cache.serialization.copy_value`): changes made to the value before the commit are not
cached, and a value the cache refuses (a live ORM object) is refused right there, before the commit.

Evictions and clears run to completion even when the calling task is cancelled meanwhile (a client
disconnect cancels the request's anyio scope, which cancels every await that follows): after the commit
the unit of work runs its callbacks shielded, and outside a unit the eviction runs in a shielded task of
its own, the cancellation re-raised once it is done. The method before it has run and may have committed,
and an eviction lost there would leave the old value cached for its whole TTL.

A deferred write registered from a task that outlived its unit (a task the unit's body started and did not
await) cannot wait for the commit any more. A put runs at once when the unit committed, and is dropped (and
logged) otherwise: its value may be a row that never commits. An eviction or a clear runs at once unless the
unit rolled back, also while the unit is still completing or when its outcome is unknown: dropping it would
leave the old value cached for its TTL if the commit succeeds. An eviction that runs while the unit is still
completing runs before its commit, though, so a concurrent reader can re-cache the old value until the TTL
expires: await the work inside the unit, or evict again after it. Either write runs outside the completed
unit, which the task can no longer use.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal, TypeVar

from pyfly.cache.namespaces import dedicated_cache
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import copy_value
from pyfly.data.transaction.context import outside_transaction

_logger = logging.getLogger("pyfly.cache")

T = TypeVar("T")

WriteErrors = Literal["raise", "log"]
"""What a failing write does: propagate (``"raise"``), or get logged and skipped (``"log"``)."""


def in_unit_of_work() -> bool:
    """Whether a unit of work is bound to the running task, so a cache write would wait for its commit."""
    from pyfly.data.transaction import current_unit_of_work

    return current_unit_of_work() is not None


class TransactionAwareCache:
    """Defers the writes of *delegate* to the commit of the current unit of work (see the module docs).

    Args:
        delegate: The cache to write to.
        on_write_error: ``"raise"`` (the default) propagates a failing write made outside a unit (a
            deferred write that fails is logged and counted by the unit, never raised). ``"log"`` logs every
            failing write at WARNING (an eviction at ERROR: stale data may be served) and carries on: the
            outcome of the business call never depends on the cache. The declarative decorators and the CQRS
            query cache use ``"log"``.
    """

    def __init__(self, delegate: CacheAdapter, *, on_write_error: WriteErrors = "raise") -> None:
        self._delegate = delegate
        self._on_write_error = on_write_error

    @property
    def delegate(self) -> CacheAdapter:
        """The cache the writes go to."""
        return self._delegate

    # -- reads (immediate, outside the caller's unit) -------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        with outside_transaction():
            return await self._delegate.get(key)

    async def exists(self, key: str) -> bool:
        with outside_transaction():
            return await self._delegate.exists(key)

    # -- writes (after commit) ------------------------------------------------------------------------------

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store *value* after the commit (at once outside a unit)."""
        if not in_unit_of_work():
            await self._attempt("put", key, lambda: self._delegate.put(key, value, ttl=ttl))
            return
        try:
            snapshot = copy_value(value)
        except Exception as error:
            self._failed("put", key, error)
            return
        await self._defer("put", key, lambda: self._delegate.put(key, snapshot, ttl=ttl))

    async def evict(self, key: str) -> bool:
        """Evict *key* after the commit (at once outside a unit). Inside a unit nothing is evicted yet, so it
        returns ``False``."""
        return bool(await self.apply("evict", key, lambda: self._delegate.evict(key)))

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict every key starting with *prefix* after the commit (at once outside a unit). Inside a unit
        nothing is evicted yet, so it returns ``0``."""
        return int(await self.apply("evict_by_prefix", prefix, lambda: self._delegate.evict_by_prefix(prefix)) or 0)

    async def clear(self) -> None:
        """Clear the cache after the commit (at once outside a unit)."""
        await self.apply("clear", "*", self._delegate.clear)

    async def apply(self, operation: str, key: str, write: Callable[[], Awaitable[T]]) -> T | None:
        """Run *write*, a write on :attr:`delegate`, as this cache runs its evictions: after the commit inside
        a unit of work (``None`` is returned then), at once outside one, and to completion either way, even
        when the calling task is cancelled meanwhile. Use it to make several writes one deferred step (they
        then run, or are dropped, together). *operation* and *key* name the write in the log when it fails."""
        if in_unit_of_work():
            await self._defer(operation, key, write)
            return None
        return await self._shielded(operation, key, write)

    # -- immediate writes (outside the caller's unit) -------------------------------------------------------

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store *value* at once when *key* is absent: its answer cannot wait for a commit. It runs outside the
        caller's unit, so the caller's rollback does not undo it."""
        with outside_transaction():
            return await self._delegate.put_if_absent(key, value, ttl=ttl)

    async def evict_if_present(self, key: str) -> bool:
        """Evict *key* at once, even inside a unit of work (Spring's ``evictIfPresent``), and outside that
        unit: the caller's rollback does not undo it."""
        with outside_transaction():
            return await self._delegate.evict(key)

    async def invalidate(self) -> None:
        """Clear the cache at once, even inside a unit of work (Spring's ``invalidate``), and outside that
        unit: the caller's rollback does not undo it."""
        with outside_transaction():
            await self._delegate.clear()

    # -- lifecycle and namespaces ---------------------------------------------------------------------------

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()

    def with_namespace(self, name: str) -> TransactionAwareCache:
        """A transaction-aware cache over the cache dedicated to *name* (see
        :func:`~pyfly.cache.namespaces.dedicated_cache`)."""
        return TransactionAwareCache(dedicated_cache(self._delegate, name), on_write_error=self._on_write_error)

    # -- internals ------------------------------------------------------------------------------------------

    async def _attempt(self, operation: str, key: str, write: Callable[[], Awaitable[T]]) -> T | None:
        if self._on_write_error == "raise":
            return await write()
        try:
            return await write()
        except Exception as error:  # noqa: BLE001 — a cache failure never changes the business outcome
            self._failed(operation, key, error)
            return None

    async def _shielded(self, operation: str, key: str, write: Callable[[], Awaitable[T]]) -> T | None:
        """Run *write* now, outside the caller's unit, to completion even when the calling task is cancelled
        meanwhile; the cancellation is re-raised once it is done."""
        from pyfly.data.transaction.template import run_shielded

        with outside_transaction():  # the shielded task copies the context: it starts outside the unit too
            result, error, cancelled = await run_shielded(self._attempt(operation, key, write))
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _defer(self, operation: str, key: str, write: Callable[[], Awaitable[Any]]) -> None:
        from pyfly.data.transaction import IllegalTransactionStateError, UnitStatus, after_commit, current_unit_of_work

        async def deferred() -> None:
            await self._attempt(operation, key, write)

        try:
            await after_commit(deferred)
        except IllegalTransactionStateError as refusal:
            # The calling task outlived its unit (the unit's body started it and did not await it): there is
            # no commit left to wait for. A put runs now only when the unit committed; an eviction runs now
            # unless it rolled back (still completing, or an unknown outcome, it may commit).
            unit = current_unit_of_work()
            status = unit.status if unit is not None else None
            if operation == "put":
                if status is UnitStatus.COMMITTED:
                    with outside_transaction():  # the task can no longer use the unit it outlived
                        await deferred()
                else:
                    self._failed(operation, key, refusal)
            elif status is UnitStatus.ROLLED_BACK:
                self._failed(operation, key, refusal)
            else:
                await self._shielded(operation, key, write)

    def _failed(self, operation: str, key: str, error: Exception) -> None:
        if self._on_write_error == "raise":
            raise error
        level = logging.WARNING if operation == "put" else logging.ERROR
        _logger.log(
            level,
            "cache_%s_skipped key=%r cache=%s: %s: %s",
            operation,
            key,
            type(self._delegate).__qualname__,
            type(error).__name__,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
