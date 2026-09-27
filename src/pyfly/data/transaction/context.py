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
"""The one ``ContextVar`` that binds units of work to the running task.

:class:`TransactionState` is immutable: the datasources that have a unit bound (or suspended), the
repository operation scopes open in this task, and whether the current boundary is read-only. The
template binds a unit by setting a new state and resets the variable with its token on exit, so the
outer state comes back exactly, after exceptions and cancellation too. Singleton beans are never mutated.

Helpers:

- :func:`current_unit_of_work` and :func:`is_transaction_active` inspect the binding;
- :func:`detached` (a function and a decorator) runs work in a task of its own with the state cleared,
  so it gets its own transactions instead of borrowing (and outliving) its caller's;
- :func:`outside_transaction` runs a block of the calling task with its units suspended, so the work in it
  gets short units of its own while the caller waits for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
from collections.abc import Callable, Coroutine, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, ParamSpec, TypeVar, overload

from pyfly.data.transaction.unit_of_work import UnitOfWork

P = ParamSpec("P")
R = TypeVar("R")


class Suspended:
    """The marker bound for a datasource whose unit is suspended (``NOT_SUPPORTED``, ``NEVER``, a
    non-transactional ``SUPPORTS``): repository calls there get auto units of their own."""

    __slots__ = ("unit",)

    def __init__(self, unit: UnitOfWork | None) -> None:
        self.unit = unit

    def __repr__(self) -> str:
        return f"Suspended({self.unit!r})"


@dataclass(frozen=True, slots=True)
class TransactionState:
    """The units bound to the current task, by datasource, innermost last."""

    units: tuple[tuple[str, UnitOfWork | Suspended], ...] = ()
    scopes: tuple[tuple[str, UnitOfWork], ...] = ()
    read_only: bool = False

    # -- reading ------------------------------------------------------------------------------------------

    def binding(self, datasource: str) -> UnitOfWork | Suspended | None:
        """The unit (or the suspension marker) bound for *datasource*."""
        for name, bound in reversed(self.units):
            if name == datasource:
                return bound
        return None

    def unit(self, datasource: str) -> UnitOfWork | None:
        """The transactional unit bound for *datasource* (``None`` when none is, or it is suspended)."""
        bound = self.binding(datasource)
        return bound if isinstance(bound, UnitOfWork) else None

    def scope(self, datasource: str) -> UnitOfWork | None:
        """The unit of the repository operation scope open for *datasource* in this task, if any."""
        for name, unit in reversed(self.scopes):
            if name == datasource:
                return unit
        return None

    def target(self, datasource: str | None = None) -> UnitOfWork | None:
        """The unit a synchronization registers on: the scope or unit of *datasource*, or the innermost one."""
        if datasource is not None:
            return self.scope(datasource) or self.unit(datasource)
        if self.scopes:
            return self.scopes[-1][1]
        for _name, bound in reversed(self.units):
            if isinstance(bound, UnitOfWork):
                return bound
        return None

    def held_units(self, datasource: str) -> list[UnitOfWork]:
        """The units of *datasource* this task keeps open while it waits: the bound one, and every unit
        it suspended on the way here (their locks stay taken until the task returns to them)."""
        bound = self.binding(datasource)
        first = bound.unit if isinstance(bound, Suspended) else bound
        out: list[UnitOfWork] = []
        current: UnitOfWork | None = first if isinstance(first, UnitOfWork) else None
        while current is not None:
            out.append(current)
            current = current.suspended
        scoped = self.scope(datasource)
        if scoped is not None and scoped not in out:
            out.append(scoped)
        return out

    # -- deriving -----------------------------------------------------------------------------------------

    def with_binding(
        self, datasource: str, bound: UnitOfWork | Suspended, *, read_only: bool | None = None
    ) -> TransactionState:
        """This state with *bound* bound for *datasource* (innermost), and no repository scope open: an
        operation scope never crosses a transaction boundary."""
        units = tuple((name, unit) for name, unit in self.units if name != datasource) + ((datasource, bound),)
        return TransactionState(units, (), self.read_only if read_only is None else read_only)

    def with_scope(self, datasource: str, unit: UnitOfWork) -> TransactionState:
        """This state with a repository operation scope on *unit* for *datasource*."""
        scopes = tuple((name, scoped) for name, scoped in self.scopes if name != datasource) + ((datasource, unit),)
        return replace(self, scopes=scopes)

    def with_read_only(self, read_only: bool) -> TransactionState:
        """This state marked read-only (or not)."""
        return replace(self, read_only=read_only)


EMPTY = TransactionState()
"""The state of a task outside every transaction."""

_STATE: ContextVar[TransactionState] = ContextVar("pyfly_transaction_state", default=EMPTY)


def current_state() -> TransactionState:
    """The transaction state of the running task."""
    return _STATE.get()


def bind_state(state: TransactionState) -> Token[TransactionState]:
    """Make *state* current; reset it with :func:`reset_state` and the returned token, in the same task."""
    return _STATE.set(state)


def reset_state(token: Token[TransactionState]) -> None:
    """Restore the state that was current before :func:`bind_state` returned *token*."""
    _STATE.reset(token)


def current_unit_of_work(datasource: str | None = None) -> UnitOfWork | None:
    """The unit a repository call on *datasource* would use now (its operation scope, else the bound
    transactional unit); with no datasource, the innermost one."""
    return current_state().target(datasource)


def is_transaction_active(datasource: str | None = None) -> bool:
    """Whether a transactional unit (not an auto unit) is active for *datasource*, or for any datasource."""
    state = current_state()
    if datasource is not None:
        unit = state.unit(datasource)
        return unit is not None and not unit.completed
    return any(isinstance(bound, UnitOfWork) and not bound.completed for _name, bound in state.units)


def is_current_transaction_read_only() -> bool:
    """Whether the current boundary is read-only (``@transactional(read_only=True)``)."""
    return current_state().read_only


# -- detached work -------------------------------------------------------------------------------------------

_DETACHED: set[asyncio.Task[Any]] = set()


def _spawn(coroutine: Coroutine[Any, Any, R], name: str | None) -> asyncio.Task[R]:
    context = contextvars.copy_context()
    context.run(_STATE.set, EMPTY)
    task = asyncio.get_running_loop().create_task(coroutine, name=name, context=context)
    _DETACHED.add(task)  # a fire-and-forget task needs a strong reference until it finishes
    task.add_done_callback(_DETACHED.discard)
    return task


@overload
def detached(target: Coroutine[Any, Any, R], /, *, name: str | None = None) -> asyncio.Task[R]: ...
@overload
def detached(
    target: Callable[P, Coroutine[Any, Any, R]], /, *, name: str | None = None
) -> Callable[P, asyncio.Task[R]]: ...
def detached(target: Any, /, *, name: str | None = None) -> Any:
    """Run work in a task of its own with the transaction state cleared.

    - ``detached(coro)`` schedules *coro* and returns its :class:`asyncio.Task`: await it, or let it run
      in the background (the task is kept referenced until it finishes).
    - ``@detached`` on a coroutine function makes every call schedule such a task.

    The work opens its own transactions (and its repository calls their own auto units) instead of joining
    the caller's unit, which may complete before the work does::

        @transactional
        async def place(self, order: Order) -> None:
            await self.orders.save(order)
            detached(self.notifier.send_receipt(order.id))   # runs outside place()'s transaction
    """
    if inspect.iscoroutine(target):
        return _spawn(target, name)
    if inspect.iscoroutinefunction(target):
        function: Callable[..., Coroutine[Any, Any, Any]] = target

        @functools.wraps(function)
        def spawner(*args: Any, **kwargs: Any) -> asyncio.Task[Any]:
            return _spawn(function(*args, **kwargs), name)

        return spawner
    raise TypeError(f"detached() takes a coroutine or a coroutine function, got {type(target).__name__}")


# -- work outside the caller's units -------------------------------------------------------------------------


@contextlib.contextmanager
def outside_transaction() -> Iterator[None]:
    """Run the block outside the units of work bound to the running task, in the task itself.

    Every unit bound to the task, and every repository operation scope open in it, is suspended for the
    block, as ``Propagation.NOT_SUPPORTED`` suspends one: :func:`~pyfly.data.transaction.infrastructure_unit`
    and repository calls in the block open short units of their own instead of joining the caller's,
    ``after_commit`` callbacks run at once, and :func:`is_transaction_active` is false. The binding comes back
    when the block exits, after an exception or a cancellation too. Outside every unit it changes nothing.

    Unlike :func:`detached`, it starts no task: it is for work the caller waits for but that must not be
    part of the caller's transaction, such as an immediate write to a database-backed cache, which the
    caller's rollback must not undo and whose row locks another request must not wait for::

        with outside_transaction():
            await cache.evict(key)   # a short unit of its own, committed before the caller's unit ends

    The caller's units stay open meanwhile, and the task still holds their connections and locks:

    - each statement in the block checks out another pooled connection of its datasource, so size the pool
      for one more connection per task that does such work inside a unit;
    - the block must not wait for a lock the caller's unit holds. On SQLite, whose database has one writer,
      a write unit the block would open on the database of a write unit this task holds is refused at once
      with :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` (it would otherwise wait
      ``busy_timeout`` for the task's own lock): the suspended units stay visible to that check.
    """
    state = _STATE.get()
    if not state.scopes and all(isinstance(bound, Suspended) for _name, bound in state.units):
        yield
        return
    token = _STATE.set(_suspended(state))
    try:
        yield
    finally:
        _STATE.reset(token)


def _suspended(state: TransactionState) -> TransactionState:
    """*state* with every unit and repository operation scope suspended (see :func:`outside_transaction`).

    Each datasource keeps what this task holds open on it in its suspension marker, as a ``NOT_SUPPORTED``
    boundary does: the bound unit (its own suspended units chain from it), or the operation scope's unit
    when no unit is bound.
    """
    held: dict[str, UnitOfWork | Suspended] = {}
    for name, bound in state.units:
        held[name] = bound if isinstance(bound, Suspended) else Suspended(bound)
    for name, unit in state.scopes:
        held.setdefault(name, Suspended(unit))
    return TransactionState(tuple(held.items()))
