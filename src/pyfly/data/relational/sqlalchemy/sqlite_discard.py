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
"""Discarding an aiosqlite connection without leaving SQLite's write lock behind.

aiosqlite runs each connection's statements on a worker thread of its own. When the pool discards a
connection (SQLAlchemy invalidates it after a cancellation hit a statement in flight, or a poisoned unit
of work drops it), three things go wrong on their own:

- aiosqlite closes the ``sqlite3`` handle without rolling back, and a handle closed while a statement is
  still pending (a cursor whose rows the cancelled fetch never read) does not close: it becomes a zombie
  that keeps its transaction, and with it ``BEGIN IMMEDIATE``'s write lock, until the garbage collector
  finalizes the statement. Every other writer meanwhile waits ``busy_timeout`` and fails with "database is
  locked";
- a statement still running on the worker thread (a long ``INSERT ... SELECT``) keeps the lock until it
  finishes;
- under an anyio cancel scope SQLAlchemy's graceful close is cancelled and the connection stopped instead;
  the graceful close then waits forever for a thread that has already exited (one leaked task per discard).

:func:`install` adds pool listeners (``invalidate`` and ``close_detached``) that take the discard over
before SQLAlchemy terminates the connection: aiosqlite's own close and stop become no-ops, and one last
call queued on the worker thread rolls back, closes the handle and ends the thread. When that call has not
run shortly after (a statement is still running), the statement is interrupted. The interrupt waits for
that moment on purpose: ``sqlite3_interrupt`` stays in force while any statement of the connection is
pending, so an interrupt sent while nothing runs would make the rollback itself fail. :func:`wait_released`
lets the code that discards a connection wait, bounded, until the lock is free.

The takeover uses aiosqlite's queue and state directly (``Connection._tx``, ``_connection``, ``_running``
and the stop sentinel), as SQLAlchemy's own aiosqlite dialect uses the queue: a connection being
terminated no longer accepts queued calls. Without them (another aiosqlite release) nothing is taken over.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
import weakref
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import Pool

__all__ = ["install", "release", "wait_released"]

_logger = logging.getLogger(__name__)

INTERRUPT_AFTER = 0.05
"""Seconds the queued discard may wait for the worker thread before the statement it runs is interrupted."""

RELEASE_TIMEOUT = 2.0
"""The longest :func:`wait_released` waits for the discard to run."""

_RELEASE = "_pyfly_lock_release"
"""The attribute of an aiosqlite connection that holds the future of its queued discard."""

_INSTALLED: weakref.WeakSet[Pool] = weakref.WeakSet()

try:
    from aiosqlite import core as _aiosqlite_core
except ImportError:  # pragma: no cover — aiosqlite is the async SQLite driver PyFly supports
    _STOP: object | None = None
else:
    _STOP = getattr(_aiosqlite_core, "_STOP_RUNNING_SENTINEL", None)


def install(engine: AsyncEngine) -> None:
    """Make every connection *engine*'s pool discards roll back and close on its worker thread
    (idempotent, per pool)."""
    pool = engine.sync_engine.pool
    if pool in _INSTALLED:
        return
    event.listen(pool, "invalidate", _on_invalidate)
    event.listen(pool, "close_detached", _on_close_detached)
    _INSTALLED.add(pool)


def _on_invalidate(dbapi_connection: Any, _record: Any, _exception: BaseException | None) -> None:
    if dbapi_connection is not None:
        release(dbapi_connection)


def _on_close_detached(dbapi_connection: Any) -> None:
    if dbapi_connection is not None:
        release(dbapi_connection)


def release(connection: Any) -> asyncio.Future[Any] | None:
    """Take over the discard of *connection* (the pool's adapted connection, or the ``aiosqlite.Connection``):
    queue one last call on its worker thread that rolls back, closes the handle and ends the thread, and
    return a future that completes when it has run (``None`` when it cannot be queued, or no event loop runs
    in this thread). Idempotent.

    The pool listeners call it before SQLAlchemy terminates the connection, while aiosqlite still runs it.
    """
    driver = getattr(connection, "driver_connection", connection)
    pending = getattr(driver, _RELEASE, None)
    if pending is not None:
        return pending if isinstance(pending, asyncio.Future) else None
    raw = getattr(driver, "_connection", None)
    queue = getattr(driver, "_tx", None)
    running = bool(getattr(driver, "_running", False))
    if _STOP is None or not isinstance(raw, sqlite3.Connection) or queue is None or not running:
        return None
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:  # the garbage collector detached it outside the event loop's thread
        loop = None
    future: asyncio.Future[Any] | None = loop.create_future() if loop is not None else None
    # From here on aiosqlite's own close() returns at once and its stop() queues nothing that still runs:
    # the call below is the connection's last one.
    driver._connection = None
    driver._running = False
    queue.put_nowait((future, functools.partial(_discard_on_worker, raw)))
    setattr(driver, _RELEASE, future if future is not None else True)
    if loop is not None and future is not None:
        loop.call_later(INTERRUPT_AFTER, _interrupt_if_busy, raw, future)
    return future


async def wait_released(connection: Any) -> None:
    """Wait, at most :data:`RELEASE_TIMEOUT` seconds, until the discard :func:`release` queued for
    *connection* has run (queue it now when the pool has not)."""
    future = release(connection)
    if future is None or future.done():
        return
    await asyncio.wait((future,), timeout=RELEASE_TIMEOUT)


def _interrupt_if_busy(raw: sqlite3.Connection, released: asyncio.Future[Any]) -> None:
    """The queued discard has not run yet: the worker thread is still running a statement. Stop it."""
    if released.done():
        return
    try:
        raw.interrupt()
    except sqlite3.Error:  # closed already: nothing is running any more
        _logger.debug("sqlite_statement_interrupt_skipped", exc_info=True)


def _discard_on_worker(raw: sqlite3.Connection) -> object:
    """Runs on aiosqlite's worker thread as the connection's last call: end the transaction, close the
    handle, and stop the thread. Never raises (nobody may be waiting for its outcome)."""
    for _attempt in range(3):
        try:
            if raw.in_transaction:
                raw.rollback()
            break
        except sqlite3.ProgrammingError:  # the handle is closed: it holds no lock
            break
        except sqlite3.Error:
            # An interrupt meant for the statement that ran before may have hit the rollback; its flag is
            # cleared once no statement runs, so the next attempt goes through.
            _logger.debug("sqlite_discard_rollback_retried", exc_info=True)
        except Exception:  # noqa: BLE001 — a discarded connection; closing it ends the transaction anyway
            _logger.debug("sqlite_discard_rollback_failed", exc_info=True)
            break
    try:
        raw.close()
    except Exception:  # noqa: BLE001 — closing is best effort; the transaction has ended
        _logger.debug("sqlite_discard_close_failed", exc_info=True)
    return _STOP
