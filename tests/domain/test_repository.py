# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Structural-protocol tests for :class:`pyfly.domain.DomainRepository`."""

from __future__ import annotations

import uuid

import pytest

from pyfly.domain import AggregateRoot, DomainRepository


class _Account(AggregateRoot[str]):
    def __init__(self, id: str | None = None, balance: int = 0) -> None:
        super().__init__(id)
        self.balance = balance


class _InMemoryAccountRepository:
    def __init__(self) -> None:
        self._store: dict[str, _Account] = {}

    async def add(self, aggregate: _Account) -> _Account:
        assert aggregate.id is not None
        self._store[aggregate.id] = aggregate
        return aggregate

    async def find(self, id: str) -> _Account | None:
        return self._store.get(id)

    async def remove(self, aggregate: _Account) -> None:
        if aggregate.id is not None:
            self._store.pop(aggregate.id, None)

    async def next_id(self) -> str:
        return f"acct-{uuid.uuid4()}"


def test_implementation_satisfies_runtime_protocol_check() -> None:
    repo = _InMemoryAccountRepository()
    assert isinstance(repo, DomainRepository)


@pytest.mark.asyncio
async def test_in_memory_repository_round_trip() -> None:
    repo = _InMemoryAccountRepository()
    new_id = await repo.next_id()
    acct = _Account(id=new_id, balance=100)

    await repo.add(acct)
    found = await repo.find(new_id)
    assert found is acct
    assert found is not None and found.balance == 100

    await repo.remove(acct)
    assert await repo.find(new_id) is None
