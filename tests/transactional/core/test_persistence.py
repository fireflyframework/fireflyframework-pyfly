# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for ExecutionState + InMemoryPersistenceProvider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.model import (
    ExecutionPattern,
    ExecutionStatus,
)
from pyfly.transactional.core.persistence import (
    ExecutionState,
    InMemoryPersistenceProvider,
    StateSerializer,
)


@pytest.fixture
def provider() -> InMemoryPersistenceProvider:
    return InMemoryPersistenceProvider()


def _state(name: str = "t", status: ExecutionStatus = ExecutionStatus.RUNNING) -> ExecutionState:
    ctx = ExecutionContext(name=name, pattern=ExecutionPattern.SAGA, input={})
    ctx.status = status
    return ExecutionState.from_context(ctx)


class TestSerialization:
    @pytest.mark.asyncio
    async def test_round_trip(self) -> None:
        state = _state()
        raw = StateSerializer.serialize(state)
        restored = StateSerializer.deserialize(raw)
        assert restored.correlation_id == state.correlation_id
        assert restored.status == state.status
        assert restored.pattern == ExecutionPattern.SAGA


class TestInMemoryProvider:
    @pytest.mark.asyncio
    async def test_save_and_find(self, provider: InMemoryPersistenceProvider) -> None:
        s = _state()
        await provider.save(s)
        found = await provider.find(s.correlation_id)
        assert found is not None and found.correlation_id == s.correlation_id

    @pytest.mark.asyncio
    async def test_find_all_filtered_by_status(self, provider: InMemoryPersistenceProvider) -> None:
        await provider.save(_state(status=ExecutionStatus.RUNNING))
        await provider.save(_state(status=ExecutionStatus.COMPLETED))
        completed = await provider.find_all(status=ExecutionStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_find_stale(self, provider: InMemoryPersistenceProvider) -> None:
        s = _state(status=ExecutionStatus.RUNNING)
        s.updated_at = datetime.now(UTC) - timedelta(hours=2)
        await provider.save(s)
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        stale = await provider.find_stale(cutoff)
        assert len(stale) == 1

    @pytest.mark.asyncio
    async def test_cleanup_removes_terminal_old_records(
        self, provider: InMemoryPersistenceProvider
    ) -> None:
        s = _state(status=ExecutionStatus.COMPLETED)
        old = datetime.now(UTC) - timedelta(days=10)
        s.updated_at = old
        s.completed_at = old
        await provider.save(s)
        cleaned = await provider.cleanup(older_than=timedelta(days=7))
        assert cleaned == 1
        assert await provider.find(s.correlation_id) is None

    @pytest.mark.asyncio
    async def test_delete(self, provider: InMemoryPersistenceProvider) -> None:
        s = _state()
        await provider.save(s)
        assert await provider.delete(s.correlation_id) is True
        assert await provider.delete("missing") is False

    @pytest.mark.asyncio
    async def test_health(self, provider: InMemoryPersistenceProvider) -> None:
        assert await provider.is_healthy() is True
