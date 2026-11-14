"""Offline regression coverage for invocation isolation."""

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from pyfly.agentic.agents.base import Agent
from pyfly.agentic.agents.builtin_middleware import CacheMiddleware
from pyfly.agentic.agents.cache import ResultCache
from pyfly.agentic.agents.context import AgentContext
from pyfly.agentic.agents.delegation import DelegationRouter, RoundRobinStrategy
from pyfly.agentic.agents.middleware import MiddlewareContext
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.agentic.pipeline.context import PipelineContext
from pyfly.agentic.pipeline.steps import AgentStep


@pytest.mark.parametrize("existing", [False, True])
async def test_stream_cache_neither_reads_nor_writes(existing):
    cache = ResultCache()
    if existing:
        cache.put("shared", "hello", "ordinary-result")
    middleware = CacheMiddleware(cache=cache)
    context = MiddlewareContext(agent_name="shared", prompt="hello", method="run_stream")
    await middleware.before_run(context)
    assert "_cache_result" not in context.metadata
    live_stream = object()
    assert await middleware.after_run(context, live_stream) is live_stream
    assert cache.get("shared", "hello") == ("ordinary-result" if existing else None)


@pytest.mark.parametrize("entry", ["pipeline", "delegation"])
async def test_shared_agent_keeps_concurrent_memories_isolated(entry):
    entered = 0
    together = asyncio.Event()

    async def respond(messages, info):
        nonlocal entered
        entered += 1
        if entered == 2:
            together.set()
        if entered <= 2:
            await together.wait()
        return ModelResponse(parts=[TextPart(messages[-1].parts[-1].content)])

    default = MemoryManager()
    first, second = MemoryManager(), MemoryManager()
    shared = Agent("shared-memory", model=FunctionModel(respond), memory=default, auto_register=False)

    async def invoke(memory, prompt):
        if entry == "pipeline":
            return await AgentStep(shared, conversation_id="same").execute(
                PipelineContext(inputs=prompt, memory=memory), {}
            )
        return await DelegationRouter([shared], RoundRobinStrategy(), memory=memory).route(
            prompt, conversation_id="same"
        )

    await asyncio.gather(invoke(first, "tenant-one"), invoke(second, "tenant-two"))
    assert shared.memory is default
    first_history = str(await first.async_get_message_history("same"))
    second_history = str(await second.async_get_message_history("same"))
    assert "tenant-one" in first_history and "tenant-two" not in first_history
    assert "tenant-two" in second_history and "tenant-one" not in second_history
    await shared.run("default-owner", conversation_id="same")
    default_history = str(await default.async_get_message_history("same"))
    assert "default-owner" in default_history and "tenant-" not in default_history


@pytest.mark.parametrize("streaming", [False, True])
async def test_explicit_none_disables_default_memory_for_one_call(streaming):
    memory = MemoryManager()
    shared = Agent("no-memory", model="test", memory=memory, auto_register=False)
    if streaming:
        async with await shared.run_stream("private", conversation_id="same", memory=None) as handle:
            await handle.get_output()
    else:
        await shared.run("private", conversation_id="same", memory=None)
    assert await memory.async_get_message_history("same") == []
    assert shared.memory is memory


@pytest.mark.parametrize("mode", ["buffered", "incremental", "guarded"])
async def test_stream_captures_borrowed_memory_before_context_is_entered(mode):
    default, first, second = MemoryManager(), MemoryManager(), MemoryManager()
    shared = Agent("stream-memory", model="test", memory=default, auto_register=False)
    a = await shared.run_stream("tenant-one", conversation_id="same", memory=first, streaming_mode=mode)
    b = await shared.run_stream("tenant-two", conversation_id="same", memory=second, streaming_mode=mode)
    async with a as handle:
        await handle.get_output()
    async with b as handle:
        await handle.get_output()
    assert "tenant-one" in str(await first.async_get_message_history("same"))
    assert "tenant-two" not in str(await first.async_get_message_history("same"))
    assert "tenant-two" in str(await second.async_get_message_history("same"))
    assert "tenant-one" not in str(await second.async_get_message_history("same"))
    assert await default.async_get_message_history("same") == []
    assert shared.memory is default


async def test_cancelled_pipeline_call_does_not_leave_borrowed_memory_attached():
    started = asyncio.Event()

    async def respond(messages, info):
        started.set()
        await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart("unreachable")])

    default, borrowed = MemoryManager(), MemoryManager()
    shared = Agent("cancel-memory", model=FunctionModel(respond), memory=default, auto_register=False)
    task = asyncio.create_task(
        AgentStep(shared, conversation_id="same").execute(PipelineContext(inputs="private", memory=borrowed), {})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shared.memory is default
    assert await borrowed.async_get_message_history("same") == []


@pytest.mark.parametrize("state", ["memory", "conversation", "deps", "context", "history", "instructions"])
async def test_stateful_requests_never_reuse_or_populate_stateless_cache(state):
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart(f"response-{calls}")])

    cache = ResultCache()
    shared = Agent(
        "state-cache", model=FunctionModel(respond), middleware=[CacheMiddleware(cache=cache)], auto_register=False
    )
    original = await shared.run("same")
    outputs = []
    for tenant in ("one", "two"):
        kwargs = {
            "memory": {"memory": MemoryManager(), "conversation_id": "same"},
            "conversation": {"conversation_id": tenant},
            "deps": {"deps": tenant},
            "context": {"context": AgentContext(correlation_id="same", metadata={"tenant": tenant})},
            "history": {"message_history": []},
            "instructions": {"instructions": tenant},
        }[state]
        outputs.append((await shared.run("same", **kwargs)).output)
    assert outputs == ["response-2", "response-3"]
    assert (await shared.run("same")).output == original.output == "response-1"
    assert len(cache) == 1


async def test_shared_cache_separates_same_named_agent_instances():
    cache = ResultCache()
    one = Agent(
        "same-name",
        model=TestModel(custom_output_text="one"),
        instructions="one",
        auto_register=False,
        middleware=[CacheMiddleware(cache=cache)],
    )
    two = Agent(
        "same-name",
        model=TestModel(custom_output_text="two"),
        instructions="two",
        auto_register=False,
        middleware=[CacheMiddleware(cache=cache)],
    )
    first = await one.run("same")
    second = await two.run("same")
    assert first.output == "one"
    assert second.output == "two"
    assert len(cache) == 2


@pytest.mark.parametrize("kwargs", [{"output_type": str}, {"usage": object()}, {"unknown_option": "value"}])
async def test_unrepresented_request_overrides_bypass_cache(kwargs):
    cache = ResultCache()
    cache.put("shared", "same", "cached")
    middleware = CacheMiddleware(cache=cache)
    context = MiddlewareContext(agent_name="shared", prompt="same", kwargs=kwargs)
    await middleware.before_run(context)
    assert "_cache_result" not in context.metadata
    await middleware.after_run(context, "fresh")
    assert cache.get("shared", "same") == "cached"


def test_sync_call_borrows_memory_and_can_disable_attached_memory():
    default, borrowed = MemoryManager(), MemoryManager()
    shared = Agent("sync-memory", model="test", memory=default, auto_register=False)
    shared.run_sync("borrowed", conversation_id="same", memory=borrowed)
    shared.run_sync("private", conversation_id="same", memory=None)
    shared.run_sync("default", conversation_id="same")
    assert "borrowed" in str(borrowed.get_message_history("same"))
    assert "private" not in str(default.get_message_history("same"))
    assert "borrowed" not in str(default.get_message_history("same"))
    assert "default" in str(default.get_message_history("same"))
    assert shared.memory is default
