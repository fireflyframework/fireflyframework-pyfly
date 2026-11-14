"""Budget admission is checked after waiting for a workflow slot."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from pyfly.agentic.exceptions import WorkflowBudgetError
from pyfly.agentic.workflows import AgentCall, WorkflowBudget, agent, stream, workflow


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("limit", [{"max_tokens": 1}, {"max_cost_usd": 0.1}])
async def test_queued_calls_recheck_budget_before_starting(streaming, limit):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Runner:
        async def run(self, prompt, **kwargs):
            calls.append(prompt)
            started.set()
            await release.wait()
            return AgentCall(output=prompt, tokens=10, cost_usd=1)

        @asynccontextmanager
        async def run_stream(self, prompt, **kwargs):
            call = await self.run(prompt, **kwargs)
            yield SimpleNamespace(call=call, output=call.output)

    async def invoke(index):
        if streaming:
            async with stream(str(index)) as handle:
                return handle.output
        return await agent(str(index))

    @workflow(register=False)
    async def run(args, ctx):
        tasks = [asyncio.create_task(invoke(i)) for i in range(4)]
        await started.wait()
        release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = await run.run(None, runner=Runner(), budget=WorkflowBudget(max_concurrent_agents=1, **limit))
    assert calls == ["0"]
    assert results[0] == "0"
    assert all(isinstance(result, WorkflowBudgetError) for result in results[1:])


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancelled_queue_waiter_does_not_consume_call_budget(streaming):
    started, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    class Runner:
        async def run(self, prompt, **kwargs):
            calls.append(prompt)
            started.set()
            await release.wait()
            return AgentCall(output=prompt)

        @asynccontextmanager
        async def run_stream(self, prompt, **kwargs):
            call = await self.run(prompt, **kwargs)
            yield SimpleNamespace(call=call, output=call.output)

    async def invoke(prompt):
        if streaming:
            async with stream(prompt) as handle:
                return handle.output
        return await agent(prompt)

    @workflow(register=False)
    async def run(args, ctx):
        first = asyncio.create_task(invoke("first"))
        await started.wait()
        cancelled = asyncio.create_task(invoke("cancelled"))
        asyncio.get_running_loop().call_soon(queued.set)
        await queued.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        await first
        return await invoke("last")

    result = await run.run(None, runner=Runner(), budget=WorkflowBudget(max_concurrent_agents=1, max_agents_total=2))
    assert result == "last"
    assert calls == ["first", "last"]
