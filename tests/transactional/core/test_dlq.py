# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the DeadLetterService + InMemoryDeadLetterStore."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.dlq import DeadLetterService, InMemoryDeadLetterStore


@pytest.mark.asyncio
async def test_capture_and_list() -> None:
    dlq = DeadLetterService(InMemoryDeadLetterStore())
    await dlq.capture(
        execution_name="orderSaga",
        correlation_id="cid-1",
        error=RuntimeError("payment_declined"),
        step_id="charge",
        input={"order": 1},
    )
    entries = await dlq.list()
    assert len(entries) == 1
    e = entries[0]
    assert e.execution_name == "orderSaga"
    assert e.error_type == "RuntimeError"
    assert e.step_id == "charge"


@pytest.mark.asyncio
async def test_filter_by_execution_name() -> None:
    dlq = DeadLetterService(InMemoryDeadLetterStore())
    await dlq.capture(execution_name="a", correlation_id="1", error=Exception("x"))
    await dlq.capture(execution_name="b", correlation_id="2", error=Exception("y"))
    only_a = await dlq.list(execution_name="a")
    assert len(only_a) == 1


@pytest.mark.asyncio
async def test_mark_retried_increments_count() -> None:
    dlq = DeadLetterService(InMemoryDeadLetterStore())
    entry = await dlq.capture(execution_name="x", correlation_id="1", error=Exception("e"))
    await dlq.mark_retried(entry.id)
    refreshed = await dlq.get(entry.id)
    assert refreshed is not None and refreshed.retry_count == 1


@pytest.mark.asyncio
async def test_delete_returns_false_when_missing() -> None:
    dlq = DeadLetterService(InMemoryDeadLetterStore())
    assert await dlq.delete("nope") is False
