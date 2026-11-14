"""Concurrent batching validates bounds and drains work on timeout."""

import asyncio

import pytest

from pyfly.agentic.pipeline.context import PipelineContext
from pyfly.agentic.pipeline.steps import BatchLLMStep


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_batch_rejects_invalid_size(size):
    with pytest.raises(ValueError, match="batch_size"):
        BatchLLMStep(None, batch_size=size)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_batch_rejects_invalid_deadline(timeout):
    with pytest.raises(ValueError, match="max_wait_seconds"):
        BatchLLMStep(None, max_wait_seconds=timeout)


def test_batch_rejects_fake_background_jobs():
    with pytest.raises(ValueError, match="wait_for_completion"):
        BatchLLMStep(None, wait_for_completion=False)


async def test_batch_deadline_cancels_and_drains_children():
    finished = []

    class PendingAgent:
        async def run(self, prompt):
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(prompt)

    step = BatchLLMStep(PendingAgent(), batch_size=2, max_wait_seconds=0.01)
    # Outer bound makes the broken implementation fail rather than hang.
    watchdog = asyncio.timeout(0.5)
    with pytest.raises(TimeoutError):
        async with watchdog:
            await step.execute(PipelineContext(inputs={}), {"prompts": ["a", "b", "c"]})
    assert sorted(finished) == ["a", "b"]
    # The step's deadline, not the enclosing test watchdog, must have fired.
    assert not watchdog.expired()
