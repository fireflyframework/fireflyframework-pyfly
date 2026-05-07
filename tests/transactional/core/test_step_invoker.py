# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the new generic StepInvoker (retry/timeout/jitter/cpu-bound)."""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from pyfly.transactional.core.argument import Input
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.exceptions import StepFailedError, StepTimeoutError
from pyfly.transactional.core.model import ExecutionPattern, RetryPolicy
from pyfly.transactional.core.step_invoker import StepInvoker


def _ctx() -> ExecutionContext:
    return ExecutionContext(name="t", pattern=ExecutionPattern.SAGA, input={"x": 1})


class TestStepInvoker:
    @pytest.mark.asyncio
    async def test_async_method_succeeds(self) -> None:
        invoker = StepInvoker()

        async def step(payload: Annotated[dict, Input()]) -> int:
            return payload["x"] * 2

        result = await invoker.invoke(
            bean=None,
            method=step,
            step_id="s",
            ctx=_ctx(),
            retry_policy=RetryPolicy(),
        )
        assert result == 2

    @pytest.mark.asyncio
    async def test_sync_method_succeeds(self) -> None:
        invoker = StepInvoker()

        def step(payload: Annotated[dict, Input()]) -> int:
            return payload["x"] + 1

        result = await invoker.invoke(bean=None, method=step, step_id="s", ctx=_ctx(), retry_policy=RetryPolicy())
        assert result == 2

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self) -> None:
        invoker = StepInvoker()
        attempts: list[int] = []

        async def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                msg = "transient"
                raise RuntimeError(msg)
            return "ok"

        result = await invoker.invoke(
            bean=None,
            method=flaky,
            step_id="s",
            ctx=_ctx(),
            retry_policy=RetryPolicy(max_attempts=5, backoff_ms=1),
        )
        assert result == "ok"
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self) -> None:
        invoker = StepInvoker()

        async def always_fails() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(StepFailedError) as exc_info:
            await invoker.invoke(
                bean=None,
                method=always_fails,
                step_id="bang",
                ctx=_ctx(),
                retry_policy=RetryPolicy(max_attempts=2, backoff_ms=1),
            )
        assert exc_info.value.step_id == "bang"
        assert exc_info.value.attempts == 2

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        invoker = StepInvoker()

        async def slow() -> None:
            await asyncio.sleep(1.0)

        with pytest.raises(StepFailedError) as exc_info:
            await invoker.invoke(
                bean=None,
                method=slow,
                step_id="slow",
                ctx=_ctx(),
                retry_policy=RetryPolicy(timeout_ms=20),
            )
        assert isinstance(exc_info.value.cause, StepTimeoutError)
