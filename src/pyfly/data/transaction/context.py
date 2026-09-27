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
  so it gets its own transactions instead of borrowing (and outliving) its caller's.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
from collections.abc import Callable, Coroutine
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
    """Whether a transaction is active for *datasource*, or for any datasource: a transactional unit, or the
    write auto unit of the repository call running now (it commits at the end of that call, and a boundary
    inside it joins it). A read auto unit is not a transaction."""
    state = current_state()
    if datasource is not None:
        unit = state.unit(datasource) or _write_auto_unit(state.scope(datasource))
        return unit is not None and not unit.completed
    if any(isinstance(bound, UnitOfWork) and not bound.completed for _name, bound in state.units):
        return True
    return any((unit := _write_auto_unit(scoped)) is not None and not unit.completed for _name, scoped in state.scopes)


def _write_auto_unit(unit: UnitOfWork | None) -> UnitOfWork | None:
    return unit if unit is not None and unit.auto and not unit.read_only else None


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
