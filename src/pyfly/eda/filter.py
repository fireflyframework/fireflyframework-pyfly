# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Event filter — predicate that decides whether to deliver an envelope."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope


@runtime_checkable
class EventFilter(Protocol):
    def accepts(self, envelope: EventEnvelope) -> bool: ...


class HeaderEventFilter:
    """Accept envelopes whose header *name* matches *pattern*."""

    def __init__(self, name: str, pattern: str) -> None:
        self._name = name
        self._regex = re.compile(pattern)

    def accepts(self, envelope: EventEnvelope) -> bool:
        return bool(self._regex.match(envelope.headers.get(self._name, "")))


class PredicateEventFilter:
    """Wrap an arbitrary callable as an :class:`EventFilter`."""

    def __init__(self, predicate: Callable[[EventEnvelope], bool]) -> None:
        self._predicate = predicate

    def accepts(self, envelope: EventEnvelope) -> bool:
        return self._predicate(envelope)
