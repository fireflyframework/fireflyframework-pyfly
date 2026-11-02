# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Output admission precedes publication, including PydanticAI streaming consumers."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pyfly.agentic.agents import Agent, OutputGuardMiddleware
from pyfly.agentic.agents.builtin_middleware import OutputGuardError
from pyfly.agentic.exceptions import AgentError
from pyfly.agentic.security import OutputGuard

CANARY = "GUARD_CANARY"


def guarded_agent(*, sanitise=False, **kwargs):
    return Agent(
        "guarded-publication",
        model=TestModel(custom_output_text=CANARY),
        middleware=[
            OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY], sanitise=sanitise), sanitise=sanitise)
        ],
        default_middleware=False,
        auto_register=False,
        **kwargs,
    )


@pytest.mark.parametrize("sync", [False, True])
async def test_blocked_result_never_reaches_memory_or_after_hooks(sync):
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    agent = guarded_agent(memory=memory)
    after = AsyncMock(side_effect=lambda ctx, result: result)
    agent.middleware.add(SimpleNamespace(after_run=after))
    with pytest.raises(OutputGuardError):
        if sync:
            agent.run_sync("hello", conversation_id="conversation")
        else:
            await agent.run("hello", conversation_id="conversation")
    memory.async_add_turn.assert_not_called()
    after.assert_not_called()


@pytest.mark.parametrize("mode", ["buffered", "guarded"])
async def test_rejected_stream_never_returns_content_handle_and_accounts_usage(mode):
    agent = guarded_agent()
    agent._record_usage = Mock()
    returned = False
    with pytest.raises(OutputGuardError):
        async with await agent.run_stream("hello", streaming_mode=mode):
            returned = True
    assert not returned
    agent._record_usage.assert_called_once()
    assert agent._record_usage.call_args.args[0].usage.requests == 1


async def test_guarded_stream_prevents_studio_consumer_events():
    agent = guarded_agent()
    events = []
    with pytest.raises(OutputGuardError):
        async with await agent.run_stream("hello") as stream:
            async for text in stream.stream_text(delta=False, debounce_by=0.05):
                events.append({"kind": "agent.output", "text": text})
            await stream.get_output()
    assert events == []


async def test_incremental_with_output_guard_rejected_before_model():
    agent = guarded_agent()
    agent._agent.run_stream = Mock(side_effect=AssertionError("provider must not start"))
    with pytest.raises(ValueError, match="guard"):
        await agent.run_stream("hello", streaming_mode="incremental")
    agent._agent.run_stream.assert_not_called()


async def test_safe_guarded_stream_preserves_native_typed_output_usage_and_messages():
    class Answer(BaseModel):
        value: int

    agent = Agent(
        "typed-guarded",
        model=TestModel(custom_output_args={"value": 42}),
        output_type=Answer,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY]))],
        default_middleware=False,
        auto_register=False,
    )
    async with await agent.run_stream("hello", streaming_mode="guarded") as stream:
        assert stream.is_complete
        output = await stream.get_output()
        assert isinstance(output, Answer)
        assert output.value == 42
        assert stream.usage.requests == 1
        assert stream.new_messages()


async def test_sanitising_stream_blocks_instead_of_exposing_raw_history():
    agent = guarded_agent(sanitise=True)
    with pytest.raises(OutputGuardError):
        async with await agent.run_stream("hello"):
            pytest.fail("unsafe raw stream history escaped")


async def test_native_sanitise_rejects_without_persisting_raw_history():
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    agent = guarded_agent(sanitise=True, memory=memory)
    with pytest.raises(OutputGuardError):
        await agent.run("hello", conversation_id="conversation")
    memory.async_add_turn.assert_not_called()


async def test_structured_unsafe_output_never_exposes_typed_history():
    class Answer(BaseModel):
        value: str

    agent = Agent(
        "unsafe-typed",
        model=TestModel(custom_output_args={"value": CANARY}),
        output_type=Answer,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY]))],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(OutputGuardError):
        async with await agent.run_stream("hello"):
            pytest.fail("typed unsafe output escaped")


async def test_blocked_reasoning_outcome_is_not_persisted():
    memory = SimpleNamespace(async_add_turn=AsyncMock())
    agent = guarded_agent(memory=memory)
    pattern = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(output=CANARY)))
    with pytest.raises(OutputGuardError):
        await agent.run_with_reasoning(pattern, "hello", conversation_id="conversation")
    memory.async_add_turn.assert_not_called()


async def test_accepted_guarded_stream_persists_after_verdict_before_handle():
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    agent = Agent(
        "accepted",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY]))],
        default_middleware=False,
        auto_register=False,
    )
    async with await agent.run_stream("hello", conversation_id="conversation") as stream:
        assert await stream.get_output() == "Accepted"
        memory.async_add_turn.assert_awaited_once()
    memory.async_add_turn.assert_awaited_once()
    assert memory.async_add_turn.call_args.args[2] == "Accepted"


@pytest.mark.parametrize("method", ["run", "run_sync", "run_stream"])
async def test_raw_provider_event_handler_rejected_before_guarded_model_call(method):
    agent = guarded_agent()
    events = []

    async def handler(ctx, stream):
        async for event in stream:
            events.append(event)

    with pytest.raises(ValueError, match="event_stream_handler"):
        if method == "run_sync":
            agent.run_sync("hello", event_stream_handler=handler)
        elif method == "run_stream":
            async with await agent.run_stream("hello", event_stream_handler=handler):
                pytest.fail("raw provider handler was accepted")
        else:
            await agent.run("hello", event_stream_handler=handler)
    assert events == []


async def test_guarded_stream_runs_admission_once():
    guard = OutputGuard(deny_patterns=[CANARY])
    guard.scan = Mock(wraps=guard.scan)
    agent = Agent(
        "once",
        model=TestModel(custom_output_text="Accepted"),
        middleware=[OutputGuardMiddleware(guard=guard)],
        default_middleware=False,
        auto_register=False,
    )
    async with await agent.run_stream("hello") as stream:
        assert await stream.get_output() == "Accepted"
    guard.scan.assert_called_once_with("Accepted")


@pytest.mark.parametrize("cancel", [True, False])
async def test_pending_guard_closes_provider_on_cancellation_or_timeout(cancel):
    waiting = asyncio.Event()
    closed = asyncio.Event()
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())

    class PendingGuard(OutputGuardMiddleware):
        async def admit_output(self, context, result):
            waiting.set()
            await asyncio.Event().wait()
            return result

    agent = Agent(
        "pending",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[PendingGuard()],
        default_middleware=False,
        auto_register=False,
    )
    agent._record_usage = Mock()
    errors = AsyncMock()
    agent.middleware.add(SimpleNamespace(on_error=errors))
    original = agent._agent.run_stream

    @asynccontextmanager
    async def tracked(*args, **kwargs):
        try:
            async with original(*args, **kwargs) as stream:
                yield stream
        finally:
            closed.set()

    agent._agent.run_stream = tracked

    async def invoke():
        async with await agent.run_stream(
            "hello", conversation_id="conversation", timeout=None if cancel else 10, guarded_timeout=0.5
        ):
            pytest.fail("pending admission exposed a handle")

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert closed.is_set()
    memory.async_add_turn.assert_not_called()
    agent._record_usage.assert_called_once()
    errors.assert_awaited_once()


async def test_after_hook_mutation_is_rejected_before_next_hook_return_or_memory():
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    observer = AsyncMock(side_effect=lambda context, result: result)

    async def mutate(context, result):
        result.output = CANARY
        return result

    agent = Agent(
        "transformed",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[
            OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY])),
            SimpleNamespace(after_run=observer),
            SimpleNamespace(after_run=mutate),
        ],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(OutputGuardError):
        await agent.run("hello", conversation_id="conversation")
    observer.assert_not_called()
    memory.async_add_turn.assert_not_called()


async def test_native_sanitise_rejects_instead_of_returning_unsafe_history_or_wrong_type():
    class Answer(BaseModel):
        value: str

    agent = Agent(
        "typed-sanitise",
        model=TestModel(custom_output_args={"value": CANARY}),
        output_type=Answer,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY], sanitise=True), sanitise=True)],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(OutputGuardError):
        await agent.run("hello")


async def test_noop_after_hook_does_not_repeat_admission():
    guard = OutputGuard(deny_patterns=[CANARY])
    guard.scan = Mock(wraps=guard.scan)
    agent = Agent(
        "noop",
        model=TestModel(custom_output_text="Accepted"),
        middleware=[
            OutputGuardMiddleware(guard=guard),
            SimpleNamespace(after_run=AsyncMock(side_effect=lambda c, r: r)),
        ],
        default_middleware=False,
        auto_register=False,
    )
    await agent.run("hello")
    guard.scan.assert_called_once_with("Accepted")


async def test_replacement_stream_admission_is_rejected():
    class Replace:
        async def admit_output(self, context, result):
            return SimpleNamespace(output="safe")

    agent = Agent(
        "replace",
        model=TestModel(custom_output_text=CANARY),
        middleware=[Replace()],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="replacement"):
        async with await agent.run_stream("hello"):
            pytest.fail("raw original stream escaped after replacement")


@pytest.mark.parametrize("limit_delta", [0, -1])
async def test_guarded_byte_limit_counts_retained_parts_not_repeated_snapshots(limit_delta):
    text = "one two three four"
    # Native UTF-8 JSON parts, including null native metadata; no response timestamps.
    exact = len(
        b'[{"content":"one two three four","id":null,"provider_name":null,"provider_details":null,"part_kind":"text"}]'
    )
    agent = Agent("bounded", model=TestModel(custom_output_text=text), default_middleware=False, auto_register=False)
    usage = RunUsage()
    agent._record_usage = Mock()
    if limit_delta < 0:
        with pytest.raises(AgentError, match="guarded_max_bytes"):
            async with await agent.run_stream(
                "hello", streaming_mode="guarded", guarded_max_bytes=exact - 1, usage=usage
            ):
                pytest.fail("overflow exposed a stream")
    else:
        async with await agent.run_stream(
            "hello", streaming_mode="guarded", guarded_max_bytes=exact, usage=usage
        ) as stream:
            assert await stream.get_output() == text
    assert usage.requests == 1
    agent._record_usage.assert_called_once()


async def test_guarded_byte_limit_includes_structured_output_tool_arguments():
    class Answer(BaseModel):
        value: str

    agent = Agent(
        "bounded-typed",
        model=TestModel(custom_output_args={"value": "x" * 500}),
        output_type=Answer,
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="guarded_max_bytes"):
        async with await agent.run_stream("hello", streaming_mode="guarded", guarded_max_bytes=100):
            pytest.fail("oversized structured arguments escaped")


@pytest.mark.parametrize(
    "field,value",
    [
        ("guarded_max_bytes", 0),
        ("guarded_max_bytes", True),
        ("guarded_max_bytes", 1.5),
        ("guarded_timeout", 0),
        ("guarded_timeout", float("inf")),
        ("guarded_timeout", float("nan")),
        ("guarded_timeout", True),
    ],
)
async def test_invalid_guarded_limits_rejected_before_provider(field, value):
    agent = guarded_agent()
    agent._agent.run_stream = Mock(side_effect=AssertionError("provider must not start"))
    with pytest.raises(ValueError, match=field):
        await agent.run_stream("hello", **{field: value})
    agent._agent.run_stream.assert_not_called()


async def test_guarded_typed_output_transformation_is_readmitted_without_changing_history():
    class Answer(BaseModel):
        value: int

    guard = OutputGuard(deny_patterns=[CANARY])
    guard.scan = Mock(wraps=guard.scan)

    async def transform(context, result):
        result.output.value = 43
        return result

    agent = Agent(
        "typed-transform",
        model=TestModel(custom_output_args={"value": 42}),
        output_type=Answer,
        middleware=[OutputGuardMiddleware(guard=guard), SimpleNamespace(after_run=transform)],
        default_middleware=False,
        auto_register=False,
    )
    result = await agent.run("hello")
    assert isinstance(result.output, Answer) and result.output.value == 43
    assert guard.scan.call_count == 2
    assert result.usage.requests == 1
    assert result.new_messages()


async def test_guarded_history_mutation_is_rejected_before_memory_or_observer():
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    observer = AsyncMock(side_effect=lambda context, result: result)

    async def mutate(context, result):
        result.all_messages()[-1].parts[0].content = CANARY
        return result

    agent = Agent(
        "history-transform",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[
            OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY])),
            SimpleNamespace(after_run=observer),
            SimpleNamespace(after_run=mutate),
        ],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="provider history"):
        await agent.run("hello", conversation_id="conversation")
    observer.assert_not_called()
    memory.async_add_turn.assert_not_called()


async def test_unsupported_guarded_snapshot_fails_closed_before_observer():
    from pydantic_ai import TextOutput

    observer = AsyncMock(side_effect=lambda context, result: result)

    def opaque(text: str) -> object:
        return object()

    agent = Agent(
        "unsupported",
        model=TestModel(custom_output_text="Accepted"),
        output_type=TextOutput(opaque),
        middleware=[
            OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY])),
            SimpleNamespace(after_run=observer),
        ],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="snapshot-compatible"):
        await agent.run("hello")
    observer.assert_not_called()


async def test_guarded_limits_bind_from_native_configuration_and_environment(monkeypatch):
    from pyfly.agentic.auto_configuration import bind_agentic_config
    from pyfly.core.config import Config

    monkeypatch.setenv("PYFLY_AGENTIC_GUARDED_STREAM_MAX_BYTES", "321")
    monkeypatch.setenv("PYFLY_AGENTIC_GUARDED_STREAM_TIMEOUT", "0.75")
    config = bind_agentic_config(Config())
    assert config.guarded_stream_max_bytes == 321
    assert config.guarded_stream_timeout == 0.75
    agent = Agent(
        "config-bound",
        model=TestModel(custom_output_text="x" * 400),
        config=config,
        middleware=[OutputGuardMiddleware()],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="guarded_max_bytes"):
        async with await agent.run_stream("hello"):
            pytest.fail("native configuration was ignored")


async def test_stream_history_transform_cannot_mutate_an_already_exposed_handle():
    held = []
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())

    async def mutate(context, result):
        result.all_messages()[-1].parts[0].content = CANARY
        return result

    agent = Agent(
        "held-history",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[
            OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY])),
            SimpleNamespace(after_run=mutate),
        ],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="provider history"):
        async with await agent.run_stream("hello", conversation_id="conversation") as stream:
            held.append(stream)
    assert held == []
    memory.async_add_turn.assert_not_called()


async def test_guarded_stream_finalizes_once_even_if_consumer_body_fails():
    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())
    observer = AsyncMock(side_effect=lambda context, result: result)
    errors = AsyncMock()
    agent = Agent(
        "consumer-body",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[OutputGuardMiddleware(), SimpleNamespace(after_run=observer, on_error=errors)],
        default_middleware=False,
        auto_register=False,
    )
    agent._record_usage = Mock()
    with pytest.raises(RuntimeError, match="consumer failed"):
        async with await agent.run_stream("hello", conversation_id="conversation"):
            observer.assert_awaited_once()
            memory.async_add_turn.assert_awaited_once()
            raise RuntimeError("consumer failed")
    observer.assert_awaited_once()
    memory.async_add_turn.assert_awaited_once()
    agent._record_usage.assert_called_once()
    errors.assert_not_called()


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("canary", [CANARY, "GUARD\nCANARY"])
async def test_guard_scans_serialized_typed_fields_hidden_from_repr(streamed, canary):
    from pydantic import Field

    class Answer(BaseModel):
        public: str
        hidden: str = Field(repr=False)

    agent = Agent(
        "hidden-field",
        model=TestModel(custom_output_args={"public": "ok", "hidden": canary}),
        output_type=Answer,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[canary]))],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(OutputGuardError):
        if streamed:
            async with await agent.run_stream("hello"):
                pytest.fail("hidden unsafe field escaped")
        else:
            await agent.run("hello")


@pytest.mark.parametrize("mutation", ["output", "history", "replacement"])
async def test_later_admission_hook_cannot_bypass_earlier_guard(mutation):
    from dataclasses import replace

    memory = SimpleNamespace(async_get_message_history=AsyncMock(return_value=[]), async_add_turn=AsyncMock())

    class MutatingAdmission:
        async def admit_output(self, context, result):
            if mutation == "output":
                result.output = CANARY
            elif mutation == "history":
                result.all_messages()[-1].parts[0].content = CANARY
            else:
                return replace(result)
            return result

    agent = Agent(
        "composed",
        model=TestModel(custom_output_text="Accepted"),
        memory=memory,
        middleware=[OutputGuardMiddleware(guard=OutputGuard(deny_patterns=[CANARY])), MutatingAdmission()],
        default_middleware=False,
        auto_register=False,
    )
    with pytest.raises(AgentError, match="admission"):
        await agent.run("hello", conversation_id="conversation")
    memory.async_add_turn.assert_not_called()
