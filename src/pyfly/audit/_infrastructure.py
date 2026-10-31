# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Execution-local trust for audit infrastructure, never a client-supplied opt-out."""

from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Protocol, overload

_infrastructure: ContextVar[tuple[object, ...]] = ContextVar("pyfly.audit.infrastructure", default=())


def has_active_data_units() -> bool:
    from pyfly.data.transaction.context import current_state
    from pyfly.data.transaction.unit_of_work import UnitStatus

    state = current_state()
    return any(
        unit.status in {UnitStatus.ACTIVE, UnitStatus.COMPLETING}
        for name, _ in (*state.units, *state.scopes)
        for unit in state.held_units(name)
    )


def _identity(owner: object) -> object:
    from pyfly.container.scoped_proxy import is_scoped_proxy, proxy_target

    seen: set[int] = set()
    for _ in range(16):
        if owner is None or id(owner) in seen:
            raise ValueError("Audit infrastructure owner did not resolve")
        seen.add(id(owner))
        if not is_scoped_proxy(owner):
            return owner
        owner = proxy_target(owner)
    raise ValueError("Audit infrastructure owner proxy nesting exceeds its bound")


@contextmanager
def audit_infrastructure(owner: object) -> Iterator[None]:
    token = _infrastructure.set((*_infrastructure.get(), _identity(owner)))
    try:
        yield
    finally:
        _infrastructure.reset(token)


def is_audit_infrastructure(owner: object) -> bool:
    identity = _identity(owner)
    return any(identity is value for value in _infrastructure.get())


class _InfrastructureDecorator(Protocol):
    def __call__[**P, R](self, function: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]: ...


@overload
def audit_infrastructure_call[**P, R](
    function: Callable[P, Awaitable[R]], *, owner: Callable[[Any], object] | None = None
) -> Callable[P, Coroutine[Any, Any, R]]: ...


@overload
def audit_infrastructure_call(*, owner: Callable[[Any], object]) -> _InfrastructureDecorator: ...


def audit_infrastructure_call(
    function: Callable[..., Awaitable[Any]] | None = None, *, owner: Callable[[Any], object] | None = None
) -> Any:
    def decorate[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            if not args:
                raise ValueError("Audit infrastructure method requires an explicit owner")
            target = owner(args[0]) if owner is not None else args[0]
            with audit_infrastructure(target):
                return await function(*args, **kwargs)

        return guarded

    return decorate if function is None else decorate(function)
