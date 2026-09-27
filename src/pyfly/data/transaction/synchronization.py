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
"""Transaction synchronizations: code that runs at the edges of a unit of work (Spring's
``TransactionSynchronization``).

Register a :class:`TransactionSynchronization` on the current unit with :func:`register_synchronization`,
or a single callback with :func:`after_commit`. The template calls them in registration order:

- ``before_commit(read_only)`` inside the unit, before it commits; an exception rolls the unit back and
  propagates;
- ``before_completion()`` right before the unit commits or rolls back;
- ``after_commit()`` once the unit has committed and released its connection;
- ``after_completion(status)`` once the unit has completed either way.

``after_commit`` and ``after_completion`` run with the transaction state cleared: they are not inside a
transaction, and repository calls in them get auto units. An exception there is logged with the unit's
datasource and counted in the ``pyfly.tx.synchronization.failures`` metric; it never turns a committed
unit into a failure. Code that needs a delivery guarantee writes to the transactional outbox instead.

``after_commit(callback)`` is the primitive behind domain-event and CQRS publication and cache writes
after commit. Outside a transaction it runs the callback at once::

    @transactional
    async def place(self, order: Order) -> None:
        await self.orders.save(order)
        await after_commit(lambda: self.events.publish(OrderPlaced(order.id)))
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.errors import IllegalTransactionStateError


class CompletionStatus(enum.Enum):
    """How a unit of work ended, as ``after_completion`` is told."""

    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"
    """The commit was interrupted in flight (:class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`)."""


class TransactionPhase(enum.Enum):
    """A point in a unit's completion that a callback can be bound to (:func:`on_phase`)."""

    BEFORE_COMMIT = "BEFORE_COMMIT"
    AFTER_COMMIT = "AFTER_COMMIT"
    AFTER_ROLLBACK = "AFTER_ROLLBACK"
    AFTER_COMPLETION = "AFTER_COMPLETION"


@runtime_checkable
class TransactionSynchronization(Protocol):
    """Callbacks a unit of work runs as it completes. Subclass :class:`TransactionSynchronizationAdapter`
    to implement only some of them."""

    async def before_commit(self, read_only: bool) -> None:
        """Inside the unit, before it commits. Raising rolls the unit back."""
        ...

    async def before_completion(self) -> None:
        """Right before the unit commits or rolls back."""
        ...

    async def after_commit(self) -> None:
        """After the unit committed, outside any transaction."""
        ...

    async def after_completion(self, status: CompletionStatus) -> None:
        """After the unit completed (committed, rolled back, or with an unknown outcome)."""
        ...


class TransactionSynchronizationAdapter:
    """A :class:`TransactionSynchronization` whose callbacks do nothing; override the ones you need."""

    async def before_commit(self, read_only: bool) -> None:
        """Nothing to do before commit."""

    async def before_completion(self) -> None:
        """Nothing to do before completion."""

    async def after_commit(self) -> None:
        """Nothing to do after commit."""

    async def after_completion(self, status: CompletionStatus) -> None:
        """Nothing to do after completion."""


Callback = Callable[[], Awaitable[Any] | Any]
"""A synchronization callback: a plain callable or a coroutine function, called with no arguments."""


async def invoke(callback: Callback) -> None:
    """Call *callback* and await its result when it is awaitable."""
    result = callback()
    if inspect.isawaitable(result):
        await result


class PhaseCallback(TransactionSynchronizationAdapter):
    """Runs one callback at one :class:`TransactionPhase` of the unit it is registered on."""

    def __init__(self, phase: TransactionPhase, callback: Callback) -> None:
        self.phase = phase
        self.callback = callback

    async def before_commit(self, read_only: bool) -> None:
        if self.phase is TransactionPhase.BEFORE_COMMIT:
            await invoke(self.callback)

    async def after_commit(self) -> None:
        if self.phase is TransactionPhase.AFTER_COMMIT:
            await invoke(self.callback)

    async def after_completion(self, status: CompletionStatus) -> None:
        if self.phase is TransactionPhase.AFTER_COMPLETION or (
            self.phase is TransactionPhase.AFTER_ROLLBACK and status is CompletionStatus.ROLLED_BACK
        ):
            await invoke(self.callback)

    def __repr__(self) -> str:
        return f"PhaseCallback({self.phase.value}, {getattr(self.callback, '__qualname__', self.callback)!r})"


def register_synchronization(synchronization: TransactionSynchronization, *, datasource: str | None = None) -> None:
    """Register *synchronization* on the current unit of work.

    The unit is the one bound for *datasource*, or the innermost bound unit when *datasource* is
    ``None`` (a repository call's auto unit counts). Raises
    :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` when there is none, a ``SUPPORTS``
    or ``NOT_SUPPORTED`` boundary without a unit included: this is a plain call, and the synchronization's
    callbacks are coroutines it cannot run at once. Check
    :func:`~pyfly.data.transaction.context.is_transaction_active` first, or use :func:`after_commit` or
    :func:`on_phase`, which run their callback at once outside a unit.
    """
    unit = current_state().target(datasource)
    if unit is None:
        where = "" if datasource is None else f" for datasource {datasource!r}"
        raise IllegalTransactionStateError(
            f"No unit of work is active{where}: a transaction synchronization needs one. Register it inside "
            "@transactional (or a TransactionTemplate block), or call after_commit(), which runs its callback "
            "at once outside a transaction.",
            datasource=datasource,
        )
    unit.check_usable()
    unit.synchronizations.append(synchronization)


async def on_phase(phase: TransactionPhase, callback: Callback, *, datasource: str | None = None) -> None:
    """Run *callback* at *phase* of the current unit; outside a unit, run it now.

    Outside a unit there is nothing to wait for: an ``AFTER_COMMIT``, ``BEFORE_COMMIT`` or
    ``AFTER_COMPLETION`` callback runs at once, and an ``AFTER_ROLLBACK`` callback does not run.
    """
    unit = current_state().target(datasource)
    if unit is None:
        if phase is not TransactionPhase.AFTER_ROLLBACK:
            await invoke(callback)
        return
    unit.check_usable()
    unit.synchronizations.append(PhaseCallback(phase, callback))


async def after_commit(callback: Callback, *, datasource: str | None = None) -> None:
    """Run *callback* after the current unit commits; outside a transaction, run it now.

    It does not run when the unit rolls back. Inside a unit, a failure in it is logged and counted, never
    raised; outside one it is a plain call, and its exception propagates.
    """
    await on_phase(TransactionPhase.AFTER_COMMIT, callback, datasource=datasource)
