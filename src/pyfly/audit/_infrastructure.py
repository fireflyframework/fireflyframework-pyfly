# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Execution-local trust for audit infrastructure, never a client-supplied opt-out."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_trusted = object()
_infrastructure: ContextVar[object | None] = ContextVar("pyfly.audit.infrastructure", default=None)


@contextmanager
def audit_infrastructure() -> Iterator[None]:
    token = _infrastructure.set(_trusted)
    try:
        yield
    finally:
        _infrastructure.reset(token)


def is_audit_infrastructure() -> bool:
    return _infrastructure.get() is _trusted


def audit_infrastructure_call[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(function)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        with audit_infrastructure():
            return await function(*args, **kwargs)

    return guarded
