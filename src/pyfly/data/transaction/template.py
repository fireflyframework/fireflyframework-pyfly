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
"""The transaction template: Spring's ``AbstractPlatformTransactionManager`` algorithm, written once.

Every transactional boundary runs through here, whatever the backend: ``@transactional``, the
programmatic :class:`TransactionTemplate`, ``reactive_transactional``, the short auto units repositories
open outside a transaction, and :func:`infrastructure_unit` for framework adapters. A
:class:`~pyfly.data.transaction.manager.TransactionManager` only opens, commits, rolls back and releases.

Programmatic use::

    template = TransactionTemplate("reporting", timeout=5)
    async with template.transaction() as unit:          # unit.resource is the AsyncSession
        await ledger.save(entry)
    total = await template.execute(ledger.recompute)    # a coroutine function run inside a unit

Completion rules:

- A participant that exits with an exception its rules roll back marks the unit rollback-only, and the
  exception propagates. The outermost boundary then rolls back; when it completes normally it raises
  :class:`~pyfly.data.transaction.errors.UnexpectedRollbackError`.
- A ``no_rollback_for`` exception commits, unless the unit is rollback-only or its transaction is no
  longer active: then it rolls back and the original exception propagates (never ``PendingRollbackError``).
- ``NESTED`` runs in a savepoint; a failure rolls back to it and does not mark the outer unit. So does a
  failure of the flush that releasing the savepoint runs (what the scope left pending): the scope's caller
  gets it, and the outer unit goes on.
- ``timeout`` bounds a new unit's body with ``asyncio.timeout``; on expiry the unit rolls back and
  :class:`~pyfly.data.transaction.errors.TransactionTimedOutError` is raised.
- Commit, rollback, savepoint release and session close are shielded: they run in their own task,
  awaited under ``asyncio`` and ``anyio`` shields until done, and a cancellation that arrived meanwhile is
  re-raised afterwards. A client disconnect in mid-transaction never returns a poisoned or leaked
  connection to the pool.
- A boundary whose body ends with a driver error after a cancel request arrived while it ran ends as
  cancelled: the unit is poisoned (its connection discarded) and ``CancelledError`` is raised, chained
  from the driver's error (:func:`~pyfly.data.transaction.unit_of_work.cancellation_replaced_by`). A
  boundary that starts in cleanup code (``except CancelledError:``, ``finally:``, anyio's shielded cleanup)
  counts only the cancel requests that arrive after it started, so its failures keep their type.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextvars import Token
from types import TracebackType
from typing import Any, TypeVar

from pyfly.data.transaction.context import (
    EMPTY,
    Suspended,
    TransactionState,
    bind_state,
    current_state,
    reset_state,
)
from pyfly.data.transaction.definition import Isolation, Propagation, TransactionDefinition
from pyfly.data.transaction.errors import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    NestedTransactionNotSupportedError,
    TransactionError,
    TransactionTimedOutError,
    UnexpectedRollbackError,
)
from pyfly.data.transaction.manager import TransactionManager
from pyfly.data.transaction.registry import installed_registry, resolve_manager
from pyfly.data.transaction.synchronization import CompletionStatus
from pyfly.data.transaction.unit_of_work import (
    UnitOfWork,
    UnitStatus,
    cancel_requests,
    cancellation_replaced_by,
    raise_cancellation,
)

_logger = logging.getLogger(__name__)

T = TypeVar("T")

SYNCHRONIZATION_FAILURES = "pyfly_tx_synchronization_failures"
"""The counter (``pyfly.tx.synchronization.failures``) a failing ``after_commit``/``after_completion``
callback increments, labeled by datasource and phase."""


# ---------------------------------------------------------------------------------------------------------
# Shielded completion
# ---------------------------------------------------------------------------------------------------------


_anyio: Any = None


def shield_scope() -> contextlib.AbstractContextManager[Any]:
    """An anyio shield when anyio is installed: under Starlette's level-triggered cancellation every await
    of a cancelled task is cancelled again, which a plain ``asyncio.shield`` does not stop."""
    global _anyio
    if _anyio is None:
        try:
            import anyio
        except ImportError:  # pragma: no cover — anyio ships with every web stack PyFly supports
            _anyio = False
        else:
            _anyio = anyio
    return _anyio.CancelScope(shield=True) if _anyio else contextlib.nullcontext()


async def run_shielded(operation: Awaitable[T]) -> tuple[T | None, BaseException | None, bool]:
    """Run *operation* to completion in its own task, whatever happens to the calling task.

    Returns ``(result, error, cancelled)``: the operation's result or exception, and whether the calling
    task was cancelled while it waited (the caller re-raises that cancellation once it is done cleaning up).
    """
    task: asyncio.Future[T] = asyncio.ensure_future(operation)
    cancelled = False
    with shield_scope():
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                cancelled = True
    if task.cancelled():
        return None, asyncio.CancelledError(), cancelled
    error = task.exception()
    if error is not None:
        return None, error, cancelled
    return task.result(), None, cancelled


async def shielded(operation: Awaitable[T]) -> T:
    """Await *operation* shielded (see :func:`run_shielded`); raise its error, then any cancellation."""
    result, error, cancelled = await run_shielded(operation)
    if error is not None:
        raise error
    if cancelled:
        raise asyncio.CancelledError
    return result  # type: ignore[return-value]


class _Outcome:
    """What completing a unit produced: its status, the error to raise (if not the body's), cancellation."""

    __slots__ = ("cancelled", "error", "status")

    def __init__(self) -> None:
        self.status = CompletionStatus.ROLLED_BACK
        self.error: BaseException | None = None
        self.cancelled = False


# ---------------------------------------------------------------------------------------------------------
# Synchronizations
# ---------------------------------------------------------------------------------------------------------


def _record_synchronization_failure(unit: UnitOfWork, phase: str, error: BaseException) -> None:
    _logger.error(
        "transaction_synchronization_failed",
        extra={"datasource": unit.datasource, "phase": phase, "unit": unit.describe()},
        exc_info=(type(error), error, error.__traceback__),
    )
    registry = installed_registry()
    recorder = registry.metrics if registry is not None else None
    if recorder is None:
        return
    try:
        counter = recorder.counter(
            SYNCHRONIZATION_FAILURES,
            "Transaction synchronization callbacks that failed after their unit completed",
            ["datasource", "phase"],
        )
        counter.labels(datasource=unit.datasource, phase=phase).inc()
    except Exception:  # noqa: BLE001 — a metrics backend must never break a completed unit
        _logger.debug("transaction_synchronization_metric_failed", exc_info=True)


async def _before_completion(unit: UnitOfWork, outcome: _Outcome) -> None:
    for synchronization in list(unit.synchronizations):
        try:
            await synchronization.before_completion()
        except asyncio.CancelledError:
            # Complete the unit first; the cancellation is re-raised once its connection is released.
            outcome.cancelled = True
        except Exception as error:  # noqa: BLE001 — Spring logs before-completion failures and goes on
            _record_synchronization_failure(unit, "before_completion", error)


async def _after_completion(unit: UnitOfWork, status: CompletionStatus) -> None:
    """Run the after-commit and after-completion callbacks with the transaction state cleared."""
    if not unit.synchronizations:
        return
    token = bind_state(EMPTY)
    try:
        if status is CompletionStatus.COMMITTED:
            for synchronization in list(unit.synchronizations):
                try:
                    await synchronization.after_commit()
                except Exception as error:  # noqa: BLE001 — never turn a committed unit into a failure
                    _record_synchronization_failure(unit, "after_commit", error)
        for synchronization in list(unit.synchronizations):
            try:
                await synchronization.after_completion(status)
            except Exception as error:  # noqa: BLE001
                _record_synchronization_failure(unit, "after_completion", error)
    finally:
        reset_state(token)


# ---------------------------------------------------------------------------------------------------------
# Completion of a unit this boundary owns
# ---------------------------------------------------------------------------------------------------------


def _log_cleanup_failure(event: str, unit: UnitOfWork, error: BaseException) -> None:
    _logger.warning(
        event,
        extra={"datasource": unit.datasource, "unit": unit.describe()},
        exc_info=(type(error), error, error.__traceback__),
    )


async def _release_quietly(unit: UnitOfWork) -> None:
    try:
        await unit.manager.release(unit)
    except Exception as error:  # noqa: BLE001 — the unit is complete; a failing close must not mask that
        _log_cleanup_failure("unit_of_work_release_failed", unit, error)


async def _rollback_and_release(unit: UnitOfWork) -> None:
    """Roll back and release, under the unit's guard; a failing rollback is logged, never raised."""
    async with unit.guard:
        try:
            await unit.manager.rollback(unit)
        except Exception as error:  # noqa: BLE001 — the cause of the rollback is the error that matters
            _log_cleanup_failure("unit_of_work_rollback_failed", unit, error)
        unit.status = UnitStatus.ROLLED_BACK
        await _release_quietly(unit)


async def _commit_and_release(unit: UnitOfWork) -> None:
    """Commit and release, under the unit's guard. A commit failure is raised after the unit is released
    (rolled back first, unless the outcome is unknown)."""
    manager = unit.manager
    async with unit.guard:
        try:
            await manager.commit(unit)
        except CommitOutcomeUnknownError:
            unit.status = UnitStatus.UNKNOWN
            await _release_quietly(unit)
            raise
        except BaseException:
            # A definite failure (a constraint at flush or commit time): the transaction did not commit.
            try:
                await manager.rollback(unit)
            except Exception as error:  # noqa: BLE001 — the commit failure is the one to report
                _logger.debug(
                    "unit_of_work_rollback_after_commit_failure",
                    exc_info=(type(error), error, error.__traceback__),
                )
            unit.status = UnitStatus.ROLLED_BACK
            await _release_quietly(unit)
            raise
        unit.status = UnitStatus.COMMITTED
        await _release_quietly(unit)


async def _rollback(unit: UnitOfWork, outcome: _Outcome) -> None:
    """Roll *unit* back and release it (one shielded task); a failing rollback is logged, never raised."""
    unit.status = UnitStatus.COMPLETING
    await _before_completion(unit, outcome)
    _result, error, cancelled = await run_shielded(_rollback_and_release(unit))
    outcome.cancelled = outcome.cancelled or cancelled
    if error is not None:
        _log_cleanup_failure("unit_of_work_rollback_failed", unit, error)
    unit.status = UnitStatus.ROLLED_BACK
    outcome.status = CompletionStatus.ROLLED_BACK


async def _commit(unit: UnitOfWork, outcome: _Outcome) -> None:
    """Commit *unit* (its before-commit callbacks first) and release it; record any failure in *outcome*."""
    try:
        for synchronization in list(unit.synchronizations):
            await synchronization.before_commit(unit.read_only)
    except BaseException as sync_error:
        outcome.error = sync_error
        await _rollback(unit, outcome)
        return
    if unit.rollback_only or unit.poisoned or not unit.manager.resource_active(unit):
        reason = unit.rollback_only_reason
        await _rollback(unit, outcome)
        message = (
            f"{unit.describe()} was marked rollback-only"
            + (f" by {type(reason).__name__}: {reason}" if isinstance(reason, BaseException) else "")
            + "; it rolled back instead of committing. A participant failed (or a statement failed) and the "
            "failure was caught; use Propagation.NESTED to try a step and carry on after it fails."
        )
        rollback_error = UnexpectedRollbackError(message, datasource=unit.datasource)
        if isinstance(reason, BaseException):
            rollback_error.__cause__ = reason
        outcome.error = rollback_error
        return
    unit.status = UnitStatus.COMPLETING
    await _before_completion(unit, outcome)
    _result, error, cancelled = await run_shielded(_commit_and_release(unit))
    outcome.cancelled = outcome.cancelled or cancelled
    outcome.error = error
    if error is None:
        outcome.status = CompletionStatus.COMMITTED
    elif isinstance(error, CommitOutcomeUnknownError):
        outcome.status = CompletionStatus.UNKNOWN
    else:
        outcome.status = CompletionStatus.ROLLED_BACK


def _poison_on_cancellation(unit: UnitOfWork, error: BaseException | None, since: int) -> bool:
    """A unit that ends because its task was cancelled discards its connection instead of rolling it back.

    The cancellation may have landed while a statement was in flight, and the driver may then be half
    closed underneath SQLAlchemy (an anyio scope re-cancels the driver's own cleanup), so nothing on that
    connection is awaited again. Closing it rolls the transaction back on the server; the pool opens a new
    connection when it next needs one.

    Returns whether *error* is a driver error that stood in for a cancellation requested after *since* (the
    task's cancel requests when the boundary started): the boundary then raises the cancellation instead
    (:func:`~pyfly.data.transaction.unit_of_work.raise_cancellation`).
    """
    if isinstance(error, asyncio.CancelledError):
        unit.poisoned = True
        return False
    if cancellation_replaced_by(error, since=since):
        unit.poisoned = True
        return True
    return False


async def _complete(unit: UnitOfWork, definition: TransactionDefinition, error: BaseException | None) -> _Outcome:
    """Complete a unit this boundary owns, after its body returned (*error* is ``None``) or raised (the
    boundary has poisoned a unit its task's cancellation interrupted)."""
    outcome = _Outcome()
    if error is None:
        await _commit(unit, outcome)
        return outcome
    own_timeout = isinstance(error, TransactionTimedOutError) and error.context.get("unit") == unit.id
    if (
        own_timeout
        or definition.rollback_on(error)
        or unit.rollback_only
        or unit.poisoned
        or not unit.manager.resource_active(unit)
    ):
        await _rollback(unit, outcome)
        return outcome
    # A no_rollback_for exception on a healthy unit: commit, and surface the original exception, unless the
    # commit itself fails (Spring: "application exception overridden by commit exception").
    await _commit(unit, outcome)
    if outcome.error is not None:
        _logger.error(
            "transaction_application_exception_overridden_by_commit_exception",
            extra={"datasource": unit.datasource},
            exc_info=(type(error), error, error.__traceback__),
        )
        outcome.error.__context__ = error
    return outcome


# ---------------------------------------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------------------------------------


_JOINING = frozenset({Propagation.REQUIRED, Propagation.SUPPORTS, Propagation.MANDATORY, Propagation.NESTED})
"""The propagations that use a bound unit."""


class _Mode(enum.Enum):
    NEW = "new"
    JOIN = "join"
    NESTED = "nested"
    NONE = "none"


class TransactionBoundary:
    """One transactional boundary as an async context manager; ``async with`` yields the unit (``None``
    when the boundary runs without one). Built by :class:`TransactionTemplate` and ``@transactional``."""

    __slots__ = ("_definition", "_manager", "_mode", "_savepoint", "_since", "_timeout", "_token", "_unit")

    def __init__(self, manager: TransactionManager, definition: TransactionDefinition) -> None:
        self._manager = manager
        self._definition = definition
        self._mode = _Mode.NONE
        self._unit: UnitOfWork | None = None
        self._token: Token[TransactionState] | None = None
        self._timeout: asyncio.Timeout | None = None
        self._savepoint: Any = None
        self._since = 0

    async def __aenter__(self) -> UnitOfWork | None:
        # The cancel requests already pending (cleanup code runs while its task is being cancelled): only
        # one that arrives while this boundary runs can have a driver error stand in for it.
        self._since = cancel_requests()
        definition = self._definition
        manager = self._manager
        datasource = manager.datasource
        state = current_state()
        bound = state.binding(datasource)
        existing = bound if isinstance(bound, UnitOfWork) else None
        propagation = definition.propagation
        if existing is not None and existing.completed:
            if propagation in _JOINING:
                existing.check_usable()  # a task that outlived its caller's unit tries to use it: fail loudly
            existing = None  # a boundary that does not join starts from no unit at all
        if existing is not None:
            if propagation is Propagation.NEVER:
                raise IllegalTransactionStateError(
                    f"Propagation.NEVER: {existing.describe()} is active", datasource=datasource
                )
            if propagation in (Propagation.REQUIRED, Propagation.SUPPORTS, Propagation.MANDATORY):
                self._join(existing)
                return existing
            if propagation is Propagation.NESTED:
                return await self._nested(existing)
            if propagation is Propagation.NOT_SUPPORTED:
                self._run_without(state, datasource, existing)
                return None
            return await self._new(state, existing)  # REQUIRES_NEW
        if propagation is Propagation.MANDATORY:
            raise IllegalTransactionStateError(
                f"Propagation.MANDATORY: no unit of work is active for datasource '{datasource}'",
                datasource=datasource,
            )
        if propagation in (Propagation.REQUIRED, Propagation.REQUIRES_NEW, Propagation.NESTED):
            return await self._new(state, None)
        # Keep what this task holds open on the datasource (a suspended unit, a repository call's auto unit)
        # in the suspension marker, so a write that would wait for its lock is recognized (SQLite).
        held = bound.unit if isinstance(bound, Suspended) else state.scope(datasource)
        self._run_without(state, datasource, held)
        return None

    def _join(self, existing: UnitOfWork) -> None:
        self._mode = _Mode.JOIN
        self._unit = existing
        definition = self._definition
        if definition.isolation is not Isolation.DEFAULT and definition.isolation is not existing.isolation:
            _logger.debug(
                "transaction_isolation_ignored_for_participant",
                extra={"datasource": existing.datasource, "requested": definition.isolation.value},
            )

    async def _nested(self, existing: UnitOfWork) -> UnitOfWork:
        if not self._manager.capabilities.supports_savepoints:
            raise NestedTransactionNotSupportedError(
                f"Propagation.NESTED needs savepoints, which the '{self._manager.capabilities.backend}' backend "
                f"of datasource '{existing.datasource}' does not have",
                datasource=existing.datasource,
            )
        self._savepoint = await self._manager.create_savepoint(existing)
        existing.savepoint_depth += 1
        self._mode = _Mode.NESTED
        self._unit = existing
        return existing

    def _run_without(self, state: TransactionState, datasource: str, suspended: UnitOfWork | None) -> None:
        self._mode = _Mode.NONE
        read_only = state.read_only or self._definition.read_only
        self._token = bind_state(state.with_binding(datasource, Suspended(suspended), read_only=read_only))

    async def _new(self, state: TransactionState, suspended: UnitOfWork | None) -> UnitOfWork:
        definition = self._definition
        manager = self._manager
        if not manager.capabilities.supports_isolation(definition.isolation):
            raise IllegalTransactionStateError(
                f"Isolation {definition.isolation.value} is not supported on datasource '{manager.datasource}' "
                f"({manager.capabilities.backend}); supported: {sorted(manager.capabilities.isolation_levels)}",
                datasource=manager.datasource,
            )
        unit = await manager.begin(definition)
        unit.suspended = suspended
        self._mode = _Mode.NEW
        self._unit = unit
        self._token = bind_state(state.with_binding(manager.datasource, unit, read_only=definition.read_only))
        if definition.timeout is not None:
            loop = asyncio.get_running_loop()
            unit.deadline = loop.time() + definition.timeout
            self._timeout = asyncio.timeout_at(unit.deadline)
            await self._timeout.__aenter__()
        return unit

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        mode = self._mode
        unit = self._unit
        if mode is _Mode.JOIN:
            assert unit is not None
            replaced = _poison_on_cancellation(unit, exc, self._since)
            if exc is not None and self._definition.rollback_on(exc):
                unit.set_rollback_only(exc)
            if replaced:
                assert exc is not None
                await raise_cancellation(exc)
            return
        if mode is _Mode.NONE:
            if self._token is not None:
                reset_state(self._token)
            return
        assert unit is not None
        if mode is _Mode.NESTED:
            await self._exit_nested(unit, exc)
            return
        await self._exit_new(unit, exc)

    async def _exit_nested(self, unit: UnitOfWork, error: BaseException | None) -> None:
        depth = unit.savepoint_depth
        manager = self._manager
        replaced = _poison_on_cancellation(unit, error, self._since)
        if unit.poisoned:
            unit.savepoint_depth = depth - 1
            unit.set_rollback_only(error)
            if replaced:
                assert error is not None
                await raise_cancellation(error)
            return
        # A failure that propagates out, or a statement that failed inside the savepoint and was caught
        # there, rolls back to the savepoint; the outer unit is not marked (Spring's NESTED semantics).
        if (
            (error is not None and self._definition.rollback_on(error))
            or unit.marked_within(depth)
            or _failed_within_savepoint(manager, unit, self._savepoint)
        ):
            unit.savepoint_depth = depth - 1
            cancelled = await self._roll_back_to_savepoint(unit, depth)
            if cancelled:
                raise asyncio.CancelledError
            return
        # Releasing flushes what the scope left pending (an added or a changed entity). That runs at the
        # scope's depth: a failure there is the scope's, as one in its body would be. It rolls the scope back
        # to its savepoint (SQLAlchemy leaves the failed savepoint open and deactivated) and reaches the
        # scope's caller; the outer unit goes on.
        _result, release_error, cancelled = await run_shielded(manager.release_savepoint(unit, self._savepoint))
        unit.savepoint_depth = depth - 1
        if release_error is not None:
            if isinstance(release_error, Exception) and not unit.poisoned:
                cancelled = await self._roll_back_to_savepoint(unit, depth) or cancelled
                if manager.is_disconnect(release_error):
                    unit.set_rollback_only(release_error)  # the connection went with the savepoint
            else:
                unit.set_rollback_only(release_error)
        if cancelled:
            raise asyncio.CancelledError
        if release_error is None:
            return
        if error is not None:
            # A no_rollback_for exception left the scope, and releasing its savepoint failed.
            _logger.error(
                "transaction_application_exception_overridden_by_commit_exception",
                extra={"datasource": unit.datasource},
                exc_info=(type(error), error, error.__traceback__),
            )
            release_error.__context__ = error
        raise release_error

    async def _roll_back_to_savepoint(self, unit: UnitOfWork, depth: int) -> bool:
        """``ROLLBACK TO SAVEPOINT`` of this ``NESTED`` scope, shielded: it forgets a rollback-only mark set
        inside the scope, or marks the outer unit when it fails. Returns whether the task was cancelled
        meanwhile."""
        _result, rollback_error, cancelled = await run_shielded(
            self._manager.rollback_to_savepoint(unit, self._savepoint)
        )
        if rollback_error is not None:
            unit.set_rollback_only(rollback_error)
        else:
            unit.savepoint_rolled_back(depth)
        return cancelled

    async def _exit_new(self, unit: UnitOfWork, error: BaseException | None) -> None:
        if isinstance(error, asyncio.CancelledError):
            unit.poisoned = True
        body_error = error
        replaced = False
        timeout = self._timeout
        if timeout is not None:
            # Counted before the deadline exits: it withdraws its own cancel request there.
            cancel_pending = cancel_requests() > self._since
            try:
                await timeout.__aexit__(type(error) if error is not None else None, error, None)
            except TimeoutError as expired:
                body_error = self._timed_out(unit, expired)
            else:
                if timeout.expired() and cancel_pending and _driver_error(error):
                    # The unit's own deadline cancelled the body and a driver error stood in for that
                    # cancellation (the deadline has withdrawn its cancel request by now): it timed out. A
                    # body that handled the deadline's cancellation itself (Task.uncancel()) and then
                    # raised keeps its own exception.
                    assert error is not None
                    unit.poisoned = True
                    body_error = self._timed_out(unit, error)
        if body_error is error:
            replaced = _poison_on_cancellation(unit, error, self._since)
        try:
            outcome = await _complete(unit, self._definition, body_error)
        finally:
            if self._token is not None:
                reset_state(self._token)
        await _after_completion(unit, outcome.status)
        if outcome.cancelled:
            raise asyncio.CancelledError
        if replaced:
            assert error is not None
            await raise_cancellation(error)
        if outcome.error is not None and outcome.error is not body_error:
            raise outcome.error
        if body_error is not error and body_error is not None:
            raise body_error

    def _timed_out(self, unit: UnitOfWork, cause: BaseException) -> TransactionTimedOutError:
        timed_out = TransactionTimedOutError(
            f"{unit.describe()} exceeded its timeout of {self._definition.timeout} s and rolled back",
            datasource=unit.datasource,
            context={"unit": unit.id},
        )
        timed_out.__cause__ = cause
        return timed_out


def _failed_within_savepoint(manager: TransactionManager, unit: UnitOfWork, savepoint: Any) -> bool:
    """Whether a statement failed inside a savepoint the application opened within *savepoint* and left
    open (the optional ``failed_within_savepoint`` of a manager whose backend has savepoints)."""
    probe = getattr(manager, "failed_within_savepoint", None)
    return bool(probe(unit, savepoint)) if callable(probe) else False


def _driver_error(error: BaseException | None) -> bool:
    """Whether *error* may be a driver's stand-in for a cancellation (an ordinary exception that is not the
    unit of work's own, nor the end of a streamed result)."""
    return isinstance(error, Exception) and not isinstance(error, (TransactionError, StopAsyncIteration, StopIteration))


# ---------------------------------------------------------------------------------------------------------
# Auto units
# ---------------------------------------------------------------------------------------------------------


class AutoUnit:
    """The short unit a call gets outside a transaction, as an async context manager yielding the unit.

    It binds a repository operation scope (not a transactional unit: ``MANDATORY`` does not see it), so
    nested repository calls and synchronizations share it. A write unit commits, a read unit ends without
    committing anything; both run their synchronizations as a committed unit's.
    """

    __slots__ = ("_autocommit", "_manager", "_read_only", "_since", "_token", "_unit")

    def __init__(self, manager: TransactionManager, *, read_only: bool, autocommit: bool | None = None) -> None:
        self._manager = manager
        self._read_only = read_only
        self._autocommit = autocommit
        self._token: Token[TransactionState] | None = None
        self._unit: UnitOfWork | None = None
        self._since = 0

    async def __aenter__(self) -> UnitOfWork:
        self._since = cancel_requests()
        unit = await self._manager.open_auto_unit(read_only=self._read_only, autocommit=self._autocommit)
        self._unit = unit
        self._token = bind_state(current_state().with_scope(self._manager.datasource, unit))
        return unit

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        unit = self._unit
        assert unit is not None
        token = self._token
        self._token = None
        await complete_auto_unit(unit, exc, since=self._since, reset=token)


async def complete_auto_unit(
    unit: UnitOfWork,
    error: BaseException | None,
    *,
    since: int,
    reset: Token[TransactionState] | None = None,
) -> None:
    """Complete an auto unit after its work returned (*error* ``None``) or raised, and release it.

    A write unit commits; a read unit ends without writing (a rollback, nothing on autocommit) and is
    reported to its synchronizations as committed. *since* is the task's
    :func:`~pyfly.data.transaction.unit_of_work.cancel_requests` when the unit was opened. *reset* is the
    token of the scope the unit was bound with, restored before the after-completion callbacks run. Raises
    the completion's own error (an ``UnexpectedRollbackError``, a commit failure), a cancellation that
    arrived meanwhile, and the cancellation a driver error in *error* stood in for; otherwise the caller
    re-raises *error* itself.
    """
    replaced = _poison_on_cancellation(unit, error, since)
    outcome = _Outcome()
    try:
        if error is not None:
            await _rollback(unit, outcome)
        elif unit.read_only:
            await _end_read(unit, outcome)
        else:
            await _commit(unit, outcome)
    finally:
        if reset is not None:
            reset_state(reset)
    await _after_completion(unit, outcome.status)
    if outcome.cancelled:
        raise asyncio.CancelledError
    if replaced:
        assert error is not None
        await raise_cancellation(error)
    if outcome.error is not None and outcome.error is not error:
        raise outcome.error


async def _end_read(unit: UnitOfWork, outcome: _Outcome) -> None:
    # Nothing was written: end the transaction the cheapest way (a rollback; nothing on autocommit), and
    # report the unit to its synchronizations as a committed one.
    try:
        for synchronization in list(unit.synchronizations):
            await synchronization.before_commit(True)
    except BaseException as error:
        outcome.error = error
        await _rollback(unit, outcome)
        return
    await _rollback(unit, outcome)
    outcome.status = CompletionStatus.COMMITTED


def auto_unit(manager: TransactionManager, *, read_only: bool, autocommit: bool | None = None) -> AutoUnit:
    """An :class:`AutoUnit` on *manager*'s datasource (see ``TransactionManager.open_auto_unit``)."""
    return AutoUnit(manager, read_only=read_only, autocommit=autocommit)


@contextlib.asynccontextmanager
async def infrastructure_unit(
    datasource: object = None, *, read_only: bool = False, single_statement: bool = False
) -> AsyncIterator[Any]:
    """Join the unit bound for *datasource*, or open a short one; yields the backend resource (an
    ``AsyncSession`` on a relational datasource).

    The join-or-own helper of framework adapters (event store, outbox, saga state, caches, locks, token
    stores): inside a business transaction their writes are part of it (no dual write), and outside one
    they get a unit of their own that commits or rolls back and always releases its connection.
    *datasource* is a name, a ``DataSource``, a ``TransactionManager`` or ``None`` (the default
    datasource). Declare ``single_statement=True`` for one statement on its own: where the backend makes it
    cheaper (PostgreSQL) it runs on an autocommit connection, one round trip instead of three::

        async with infrastructure_unit(self._datasource, single_statement=True) as session:
            await session.execute(insert(outbox).values(...))
    """
    manager = resolve_manager(datasource)
    state = current_state()
    joined = state.scope(manager.datasource) or state.unit(manager.datasource)
    if joined is not None:
        joined.check_usable()
        token = bind_state(state.with_scope(manager.datasource, joined))
        try:
            yield joined.resource
        finally:
            reset_state(token)
        return
    async with AutoUnit(manager, read_only=read_only, autocommit=True if single_statement else None) as unit:
        yield unit.resource


# ---------------------------------------------------------------------------------------------------------
# The programmatic API
# ---------------------------------------------------------------------------------------------------------


class TransactionTemplate:
    """Programmatic transactions, with the same semantics as ``@transactional``.

    *manager* is a :class:`~pyfly.data.transaction.manager.TransactionManager`, a datasource name, a
    resource the application built (an ``async_sessionmaker``) or ``None``; it is resolved at each use.
    *settings* are :class:`~pyfly.data.transaction.definition.TransactionDefinition` fields
    (``propagation``, ``isolation``, ``read_only``, ``timeout``, ``rollback_for``, ``no_rollback_for``,
    ``datasource``, ``name``), overridable per call.

    The datasource can be named either way: ``TransactionTemplate("reporting")``,
    ``TransactionTemplate(datasource="reporting")`` or ``template.transaction(datasource="reporting")``.
    With neither, the template runs on the default datasource. A *manager* and a ``datasource`` setting
    that name different datasources raise
    :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` when the template is used.
    """

    def __init__(self, manager: object = None, **settings: Any) -> None:
        self._target = manager
        self._definition = TransactionDefinition(**settings)

    @property
    def definition(self) -> TransactionDefinition:
        """The template's default settings."""
        return self._definition

    def manager(self) -> TransactionManager:
        """The transaction manager this template runs on, resolved now."""
        return self._manager_for(self._definition)

    def _manager_for(self, definition: TransactionDefinition) -> TransactionManager:
        name = definition.datasource
        if self._target is None:
            return resolve_manager(name)
        manager = resolve_manager(self._target)
        if name is not None and manager.datasource != name:
            raise IllegalTransactionStateError(
                f"TransactionTemplate runs on datasource {manager.datasource!r} (its manager argument) but its "
                f"definition names datasource {name!r}; name the datasource once.",
                datasource=name,
            )
        return manager

    def _definition_with(self, overrides: dict[str, Any]) -> TransactionDefinition:
        if not overrides:
            return self._definition
        return dataclasses.replace(self._definition, **overrides)

    def transaction(self, **overrides: Any) -> TransactionBoundary:
        """An ``async with`` block that runs as one boundary; it yields the unit (``None`` without one)."""
        definition = self._definition_with(overrides)
        return TransactionBoundary(self._manager_for(definition), definition)

    async def execute(self, function: Callable[..., Coroutine[Any, Any, T]], /, *args: Any, **kwargs: Any) -> T:
        """Await ``function(*args, **kwargs)`` inside one boundary and return its result."""
        async with self.transaction():
            return await function(*args, **kwargs)


async def execute_in_transaction(
    manager: TransactionManager,
    definition: TransactionDefinition,
    function: Callable[..., Coroutine[Any, Any, T]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> T:
    """Await *function* inside one boundary on *manager* (what ``@transactional`` does per call)."""
    async with TransactionBoundary(manager, definition):
        return await function(*args, **kwargs)
