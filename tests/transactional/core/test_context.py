# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for ExecutionContext (signals, variables, step records, persistence)."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.model import (
    ExecutionPattern,
    ExecutionStatus,
    StepStatus,
    TccPhase,
)


def _ctx(name: str = "test") -> ExecutionContext:
    return ExecutionContext(name=name, pattern=ExecutionPattern.SAGA, input={"x": 1})


class TestStepLifecycle:
    @pytest.mark.asyncio
    async def test_step_started_and_succeeded(self) -> None:
        ctx = _ctx()
        await ctx.record_step_started("s1")
        record = ctx.get_step("s1")
        assert record is not None
        assert record.status == StepStatus.RUNNING
        assert record.attempts == 1
        await ctx.record_step_success("s1", {"ok": True}, latency_ms=12.3)
        record = ctx.get_step("s1")
        assert record is not None
        assert record.status == StepStatus.DONE
        assert ctx.get_step_result("s1") == {"ok": True}
        assert ctx.is_step_done("s1")

    @pytest.mark.asyncio
    async def test_step_failure_records_error(self) -> None:
        ctx = _ctx()
        await ctx.record_step_started("boom")
        await ctx.record_step_failure("boom", RuntimeError("nope"), latency_ms=1.0)
        record = ctx.get_step("boom")
        assert record is not None
        assert record.status == StepStatus.FAILED
        assert "nope" in (record.error or "")

    @pytest.mark.asyncio
    async def test_step_compensated_with_error(self) -> None:
        ctx = _ctx()
        await ctx.record_step_compensated("s", "result", error=ValueError("bad"))
        record = ctx.get_step("s")
        assert record is not None
        assert record.status == StepStatus.COMPENSATION_FAILED
        assert "bad" in (record.compensation_error or "")


class TestVariables:
    @pytest.mark.asyncio
    async def test_set_and_get(self) -> None:
        ctx = _ctx()
        await ctx.set_variable("k", "v")
        assert ctx.get_variable("k") == "v"
        assert ctx.get_variable("missing", "default") == "default"
        assert ctx.get_all_variables() == {"k": "v"}


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_first_call_wins(self) -> None:
        ctx = _ctx()
        first = await ctx.remember_idempotency("abc")
        second = await ctx.remember_idempotency("abc")
        assert first is True
        assert second is False
        assert ctx.has_idempotency("abc")


class TestTccPhase:
    @pytest.mark.asyncio
    async def test_phase_and_try_results(self) -> None:
        ctx = _ctx()
        await ctx.set_tcc_phase(TccPhase.TRY)
        await ctx.record_try_result("p1", {"reserved": True})
        assert ctx.tcc_phase == TccPhase.TRY
        assert ctx.get_try_result("p1") == {"reserved": True}
        assert ctx.get_all_try_results() == {"p1": {"reserved": True}}


class TestSignalDelivery:
    @pytest.mark.asyncio
    async def test_signal_buffered_when_no_waiter(self) -> None:
        ctx = _ctx()
        delivered = await ctx.deliver_signal("approved", {"by": "boss"})
        assert delivered is False
        # Subsequent wait should return immediately.
        result = await ctx.wait_for_signal("approved", timeout_ms=100)
        assert result == {"by": "boss"}

    @pytest.mark.asyncio
    async def test_signal_delivered_to_waiter(self) -> None:
        ctx = _ctx()

        async def waiter() -> object:
            return await ctx.wait_for_signal("ping", timeout_ms=1000)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        delivered = await ctx.deliver_signal("ping", "pong")
        result = await task
        assert delivered is True
        assert result == "pong"

    @pytest.mark.asyncio
    async def test_signal_timeout(self) -> None:
        ctx = _ctx()
        with pytest.raises(TimeoutError):
            await ctx.wait_for_signal("never", timeout_ms=10)


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_set_status_records_completion(self) -> None:
        ctx = _ctx()
        await ctx.set_status(ExecutionStatus.RUNNING)
        assert ctx.completed_at is None
        await ctx.set_status(ExecutionStatus.COMPLETED)
        assert ctx.completed_at is not None


class TestSerialization:
    @pytest.mark.asyncio
    async def test_round_trip_preserves_state(self) -> None:
        ctx = _ctx()
        await ctx.set_variable("k", 42)
        await ctx.record_step_started("s")
        await ctx.record_step_success("s", "ok", 5.0)
        await ctx.set_status(ExecutionStatus.COMPLETED)
        data = ctx.to_dict()
        restored = ExecutionContext.from_dict(data)
        assert restored.correlation_id == ctx.correlation_id
        assert restored.status == ExecutionStatus.COMPLETED
        assert restored.get_variable("k") == 42
        record = restored.get_step("s")
        assert record is not None and record.result == "ok"
