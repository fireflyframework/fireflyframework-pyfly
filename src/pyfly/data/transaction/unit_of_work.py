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
"""The unit of work: one transaction (or one auto unit) on one datasource, bound to the running task.

A :class:`UnitOfWork` carries the backend resource (an ``AsyncSession``, a Mongo ``ClientSession``), the
datasource it belongs to, the task that opened it, its status, the rollback-only flag, its read-only flag
and isolation, its deadline, its synchronizations and its savepoint depth.

Every ``asyncio`` task created inside a transaction inherits the binding (``ContextVar`` semantics), so a
child task may use its parent's unit. That is made safe here:

- Every operation on the resource runs under the unit's **operation guard**, a lock that is reentrant per
  task: ``gather()`` fan-out inside ``@transactional`` is serialized instead of corrupting the session. The
  guard is held for one operation (one execute, flush, commit or stream fetch), never across user code, so
  it cannot deadlock.
- A task that uses a unit that already completed gets
  :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` naming the unit, instead of writing
  into a transaction nobody will commit. Work that must outlive its transaction runs through
  :func:`~pyfly.data.transaction.context.detached`.
- A statement that fails marks the unit rollback-only when its backend says the failure leaves the
  transaction unusable (every driver error on a relational backend), so a caught failure cannot commit
  partial work; the outermost boundary then rolls back and raises
  :class:`~pyfly.data.transaction.errors.UnexpectedRollbackError`.
- A cancellation that lands while an operation is in flight marks the unit *poisoned*: its connection is
  in an unknown state, so the backend discards it instead of returning it to the pool. A driver error that
  takes the place of the cancellation (aiosqlite's ``ValueError('Connection closed')`` once an anyio scope
  re-cancelled SQLAlchemy's own cleanup, asyncmy's ``InterfaceError('Cancelled during execution')``) is
  turned back into the cancellation (:func:`cancellation_replaced_by`), so a cancel scope still catches it.
  Only a cancel request that arrived while the operation ran counts: cleanup code that runs while its task
  is still being cancelled (``except CancelledError:``, ``finally:``, anyio's shielded cleanup) sees its
  own failures as themselves.
"""

from __future__ import annotations

import asyncio
import enum
import itertools
from types import TracebackType
from typing import TYPE_CHECKING, Any, NoReturn

from pyfly.data.transaction.definition import Isolation, TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError, TransactionError

if TYPE_CHECKING:
    from pyfly.data.transaction.manager import TransactionManager
    from pyfly.data.transaction.synchronization import TransactionSynchronization

_IDS = itertools.count(1)


def cancel_requests() -> int:
    """The cancel requests pending on the running task (``Task.cancelling()``; ``0`` outside a task).

    Recorded when an operation, a boundary or an auto unit starts, it is the baseline
    :func:`cancellation_replaced_by` compares against.
    """
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


def cancellation_replaced_by(error: BaseException | None, *, since: int) -> bool:
    """Whether *error* stands in for a cancellation of the running task requested after *since*.

    When a task is cancelled (a cancel scope expired, ``wait_for`` timed out, a client disconnected) while a
    statement is in flight, a driver can raise its own error instead of the ``CancelledError``: anyio
    re-cancels SQLAlchemy's cleanup of the interrupted statement, aiosqlite then refuses the rollback with
    ``ValueError('Connection closed')``, and asyncmy reports ``InterfaceError('Cancelled during
    execution')``. Such an error must end the task as cancelled, or the cancel scope cannot catch it.

    *since* is :func:`cancel_requests` when the operation (or the unit) started: only a cancel request that
    arrived after it counts. Cleanup code runs while its task is still being cancelled (``Task.cancelling()``
    stays above zero in ``except CancelledError:``, in ``finally:`` and in anyio's
    ``with CancelScope(shield=True):`` until the cancel scope exits), and an ordinary failure of the data
    access done there, or a business exception it raises, is its own outcome. The unit of work's own errors,
    and the end of a streamed result, never stand in for a cancellation.
    """
    if (
        error is None
        or not isinstance(error, Exception)
        or isinstance(error, (TransactionError, StopAsyncIteration, StopIteration))
    ):
        return False
    return cancel_requests() > since


def _cancellation_behind(error: BaseException) -> asyncio.CancelledError | None:
    """The ``CancelledError`` *error* was raised while handling (its ``__context__``/``__cause__`` chain)."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            return current
        pending.extend(linked for linked in (current.__cause__, current.__context__) if linked is not None)
    return None


def cancelled_from(error: BaseException) -> asyncio.CancelledError:
    """The ``CancelledError`` to raise in place of *error* (see :func:`cancellation_replaced_by`).

    It carries the arguments of the cancellation *error* replaced, when its chain holds one: anyio
    recognizes its own cancellation by that message, so the cancel scope that fired catches it. It is
    chained from *error*, so the driver's error stays in the traceback.
    """
    original = _cancellation_behind(error)
    cancelled = asyncio.CancelledError(*original.args) if original is not None else asyncio.CancelledError()
    cancelled.__cause__ = error
    return cancelled


async def raise_cancellation(error: BaseException) -> NoReturn:
    """Raise the cancellation *error* stood in for (:func:`cancelled_from`).

    When *error* carries no trace of it, the running task's cancel scope is given one more chance to
    deliver its own (anyio re-delivers a scope's cancellation at every await; native ``asyncio`` delivered
    it already), so the scope that fired still recognizes and catches it.
    """
    if _cancellation_behind(error) is None:
        await asyncio.sleep(0)
    raise cancelled_from(error)


class UnitStatus(enum.Enum):
    """Where a unit of work is in its life."""

    ACTIVE = "ACTIVE"
    COMPLETING = "COMPLETING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


class OperationGuard:
    """A lock that the task holding it may take again (reentrant per task), and other tasks wait for."""

    __slots__ = ("_depth", "_lock", "_owner")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    @property
    def owner(self) -> asyncio.Task[Any] | None:
        """The task holding the guard, if any."""
        return self._owner

    async def acquire(self) -> None:
        """Take the guard, waiting while another task holds it."""
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        """Give back one level of the guard."""
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> OperationGuard:
        await self.acquire()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


class _Operation:
    """One guarded operation on a unit's resource (see :meth:`UnitOfWork.operation`)."""

    __slots__ = ("_since", "_unit")

    def __init__(self, unit: UnitOfWork) -> None:
        self._unit = unit
        self._since = 0

    async def __aenter__(self) -> UnitOfWork:
        unit = self._unit
        self._since = cancel_requests()
        unit.check_usable()
        await unit.guard.acquire()
        try:
            unit.check_usable()  # it may have completed while this task waited for the guard
        except BaseException:
            unit.guard.release()
            raise
        return unit

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        unit = self._unit
        unit.guard.release()
        if exc is None:
            return
        if cancellation_replaced_by(exc, since=self._since):
            # The driver raised its own error in place of a cancellation that arrived while the operation ran:
            # the connection is in an unknown state, and the caller must see the cancellation.
            unit.poisoned = True
            await raise_cancellation(exc)
        unit.operation_failed(exc)


class UnitOfWork:
    """One transaction, or one auto unit, on one datasource. Created by a
    :class:`~pyfly.data.transaction.manager.TransactionManager`; bound by the template."""

    def __init__(
        self,
        manager: TransactionManager,
        datasource: str,
        resource: Any,
        *,
        definition: TransactionDefinition | None = None,
        auto: bool = False,
        read_only: bool | None = None,
        deadline: float | None = None,
    ) -> None:
        self.id = next(_IDS)
        self.manager = manager
        self.datasource = datasource
        self.resource = resource
        self.definition = definition if definition is not None else TransactionDefinition(read_only=bool(read_only))
        self.auto = auto
        self.read_only = self.definition.read_only if read_only is None else read_only
        self.isolation: Isolation = self.definition.isolation
        self.deadline = deadline
        self.owner_task: asyncio.Task[Any] | None = asyncio.current_task()
        self.status = UnitStatus.ACTIVE
        self.synchronizations: list[TransactionSynchronization] = []
        self.savepoint_depth = 0
        self.guard = OperationGuard()
        self.poisoned = False
        #: The unit this one suspended (``REQUIRES_NEW``), for diagnostics and lock-cycle detection.
        self.suspended: UnitOfWork | None = None
        #: Backend-private state (the relational manager keeps the connection's options here).
        self.attributes: dict[str, Any] = {}
        self._rollback_only_depth: int | None = None
        self._rollback_only_reason: BaseException | str | None = None

    # -- state ------------------------------------------------------------------------------------------

    @property
    def new_transaction(self) -> bool:
        """Whether this unit began a transaction of its own (always true: participants share the unit)."""
        return True

    @property
    def completed(self) -> bool:
        """Whether the unit has left ``ACTIVE``: it is committing, committed, rolled back or unknown."""
        return self.status is not UnitStatus.ACTIVE

    @property
    def rollback_only(self) -> bool:
        """Whether the unit can only roll back (a participant failed, or a statement failed)."""
        return self._rollback_only_depth is not None

    @property
    def rollback_only_reason(self) -> BaseException | str | None:
        """What marked the unit rollback-only: the failure, or a message."""
        return self._rollback_only_reason

    def set_rollback_only(self, reason: BaseException | str | None = None, *, depth: int | None = None) -> None:
        """Mark the unit rollback-only; the outermost boundary will roll it back.

        A mark set inside a savepoint (``NESTED``) is cleared when the savepoint rolls back, as Spring
        resets its connection holder's flag there. *depth* places the mark in the savepoint at that depth
        (``0``: the unit itself) instead of the current one.
        """
        level = self.savepoint_depth if depth is None else depth
        if self._rollback_only_depth is None or level < self._rollback_only_depth:
            self._rollback_only_depth = level
            self._rollback_only_reason = reason

    def marked_within(self, depth: int) -> bool:
        """Whether the rollback-only mark was set inside the savepoint at *depth* (or a deeper one)."""
        return self._rollback_only_depth is not None and self._rollback_only_depth >= depth

    def savepoint_rolled_back(self, depth: int) -> None:
        """A savepoint at *depth* rolled back: forget a rollback-only mark set at or below it."""
        if self.marked_within(depth):
            self._rollback_only_depth = None
            self._rollback_only_reason = None

    # -- operations ---------------------------------------------------------------------------------------

    def check_usable(self) -> None:
        """Raise :class:`IllegalTransactionStateError` when the unit is no longer active."""
        if self.status is not UnitStatus.ACTIVE:
            raise IllegalTransactionStateError(
                f"{self.describe()} is already {self.status.value.lower()}; the calling task outlived its "
                "transaction. Await the work inside the transaction, or run it with "
                "pyfly.data.transaction.detached() so it gets transactions of its own.",
                datasource=self.datasource,
            )

    def operation(self) -> _Operation:
        """An async context manager around one operation on the resource: it takes the operation guard,
        refuses a completed unit, and records a failure (rollback-only, or poisoned on cancellation)."""
        return _Operation(self)

    def operation_failed(self, error: BaseException) -> None:
        """Record that an operation on the resource raised *error*."""
        if isinstance(error, (StopAsyncIteration, StopIteration)):
            return  # the end of a streamed result, not a failure
        if not isinstance(error, Exception):
            # Cancelled (or interrupted) while the operation was in flight: the connection is in an
            # unknown state and must not go back to the pool.
            self.poisoned = True
            return
        if self.manager.marks_rollback_only(self, error):
            self.set_rollback_only(error)

    # -- synchronizations ----------------------------------------------------------------------------------

    def register_synchronization(self, synchronization: TransactionSynchronization) -> None:
        """Add *synchronization* to this unit (see :func:`~pyfly.data.transaction.register_synchronization`)."""
        self.check_usable()
        self.synchronizations.append(synchronization)

    # -- diagnostics ----------------------------------------------------------------------------------------

    def describe(self) -> str:
        """A one-line description for errors and logs."""
        kind = "auto unit" if self.auto else f"unit of work #{self.id}"
        owner = self.owner_task.get_name() if self.owner_task is not None else "no task"
        label = f" '{self.definition.name}'" if self.definition.name else ""
        return f"{kind}{label} on datasource '{self.datasource}' (opened by task {owner})"

    def __repr__(self) -> str:
        flags = []
        if self.read_only:
            flags.append("read-only")
        if self.rollback_only:
            flags.append("rollback-only")
        if self.poisoned:
            flags.append("poisoned")
        extra = f", {', '.join(flags)}" if flags else ""
        return f"UnitOfWork(#{self.id}, datasource={self.datasource!r}, {self.status.value}{extra})"
