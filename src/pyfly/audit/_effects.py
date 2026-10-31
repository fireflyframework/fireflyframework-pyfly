# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Trusted execution fencing after committed intent; this never suppresses capture."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_admitted: ContextVar[tuple[object, ...]] = ContextVar("pyfly.audit.admitted_effects", default=())


@contextmanager
def admitted_audit_effect(owner: object) -> Iterator[None]:
    token = _admitted.set((*_admitted.get(), owner))
    try:
        yield
    finally:
        _admitted.reset(token)


def has_admitted_audit_effect() -> bool:
    return bool(_admitted.get())
