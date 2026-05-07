# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Rule-set repository SPI + in-memory adapter."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from pyfly.rule_engine.dsl import RuleSet


@runtime_checkable
class RuleSetRepository(Protocol):
    async def save(self, ruleset: RuleSet) -> None: ...
    async def get(self, ruleset_id: str) -> RuleSet | None: ...
    async def list(self) -> list[RuleSet]: ...
    async def delete(self, ruleset_id: str) -> bool: ...


class InMemoryRuleSetRepository:
    def __init__(self) -> None:
        self._store: dict[str, RuleSet] = {}
        self._lock = asyncio.Lock()

    async def save(self, ruleset: RuleSet) -> None:
        async with self._lock:
            self._store[ruleset.id] = ruleset

    async def get(self, ruleset_id: str) -> RuleSet | None:
        async with self._lock:
            return self._store.get(ruleset_id)

    async def list(self) -> list[RuleSet]:
        async with self._lock:
            return list(self._store.values())

    async def delete(self, ruleset_id: str) -> bool:
        async with self._lock:
            return self._store.pop(ruleset_id, None) is not None
