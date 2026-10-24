"""Native runs report middleware failures and clean up each context once."""

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from pyfly.agentic import AgenticConfig, AgenticRuntime
from pyfly.agentic.agents.middleware import MiddlewareChain, MiddlewareContext
from pyfly.agentic.observability.events import AgentEvent
from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher


@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("method", ["run", "sync", "cached", "stream"])
async def test_native_hook_failure_emits_one_error_and_cleans_up_in_reverse(stage, cancel, method):
    events, cleanup = [], []
    bus = ApplicationEventBus()

    async def observe(event):
        events.append(event.event_type)

    bus.subscribe(AgentEvent, observe)
    failure = asyncio.CancelledError("hook cancelled") if cancel else ValueError("hook failed")
    contexts = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def before_run(self, context):
            if not contexts:
                contexts.append(context)

        async def after_run(self, context, result):
            return result

        async def on_error(self, context, exc):
            assert exc is failure
            cleanup.append(self.name)

    class Failure(Resource):
        async def before_run(self, context):
            await super().before_run(context)
            if stage == "before":
                raise failure
            if method == "cached":
                context.metadata["_cache_result"] = object()

        async def after_run(self, context, result):
            if stage == "after":
                raise failure
            return result

    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), event_publisher=ApplicationEventPublisher(bus))
    agent = runtime.agent_factory.create(
        "middleware-failure",
        model=TestModel(),
        middleware=[Resource("first"), Failure("failure"), Resource("last")],
        default_middleware=False,
    )
    await runtime.start()
    try:
        with pytest.raises(type(failure)) as caught:
            if method == "sync":
                await asyncio.to_thread(agent.run_sync, "hello")
            elif method == "stream":
                manager = await agent.run_stream("hello")
                async with manager as stream:
                    async for _ in stream.stream_text():
                        pass
            else:
                await agent.run("hello")
        if not (cancel and method == "sync"):
            assert caught.value is failure
        assert events == ["agent.started", "agent.error"]
        assert cleanup == ["last", "failure", "first"]
        with runtime.activate():
            await agent.middleware.run_error(contexts[0], failure)
        assert cleanup == ["last", "failure", "first"]
        assert events == ["agent.started", "agent.error"]
    finally:
        await runtime.aclose()


async def test_chain_secondary_cleanup_cancellation_keeps_original_failure_and_finishes_cleanup():
    calls = []
    original = ValueError("original hook failure")

    class First:
        async def on_error(self, context, exc):
            calls.append("first")

    class Failing:
        async def before_run(self, context):
            raise original

        async def on_error(self, context, exc):
            calls.append("failing")
            raise asyncio.CancelledError("secondary cleanup cancellation")

    chain = MiddlewareChain([First(), Failing()])
    context = MiddlewareContext(agent_name="failure", prompt="hello")
    with pytest.raises(ValueError) as caught:
        await chain.run_before(context)
    assert caught.value is original
    assert calls == ["failing", "first"]
    await chain.run_error(context, original)
    assert calls == ["failing", "first"]


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_task_cancellation_during_native_hook_reports_error_once(stage):
    received, cleanup = [], []
    entered = asyncio.Event()
    bus = ApplicationEventBus()

    async def observe(event):
        received.append(event.event_type)

    bus.subscribe(AgentEvent, observe)

    class WaitingHook:
        async def before_run(self, context):
            if stage == "before":
                entered.set()
                await asyncio.Event().wait()

        async def after_run(self, context, result):
            if stage == "after":
                entered.set()
                await asyncio.Event().wait()
            return result

        async def on_error(self, context, exc):
            cleanup.append(exc)

    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), event_publisher=ApplicationEventPublisher(bus))
    await runtime.start()
    agent = runtime.agent_factory.create(
        "cancel-hook", model=TestModel(), middleware=[WaitingHook()], default_middleware=False
    )
    task = asyncio.create_task(agent.run("hello"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert len(cleanup) == 1
        assert isinstance(cleanup[0], asyncio.CancelledError)
        assert received == ["agent.started", "agent.error"]
    finally:
        await runtime.aclose()


async def test_new_task_cancellation_during_error_cleanup_propagates_after_remaining_hooks():
    entered = asyncio.Event()
    cleanup = []

    class Remaining:
        async def on_error(self, context, exc):
            cleanup.append("remaining")

    class Failing:
        async def before_run(self, context):
            raise ValueError("initial failure")

        async def on_error(self, context, exc):
            cleanup.append("cancelled cleanup")
            entered.set()
            await asyncio.Event().wait()

    chain = MiddlewareChain([Remaining(), Failing()])
    task = asyncio.create_task(chain.run_before(MiddlewareContext(agent_name="cancel-cleanup", prompt="hello")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert cleanup == ["cancelled cleanup", "remaining"]
