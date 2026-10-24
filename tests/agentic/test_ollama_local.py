"""Explicit live acceptance of native PyFly APIs against local Ollama only.

Run ``pytest tests/agentic/test_ollama_local.py -m ollama``. Missing local
infrastructure is a failure, so this opt-in acceptance cannot silently skip.
All names, records and knowledge documents below are fictional fixtures.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
from typing import Any
from urllib.request import urlopen

import pytest
from pydantic import BaseModel
from pydantic_ai import DeferredToolResults

from pyfly.agentic import Agent, AgentFactory, AgenticConfig, AgenticRuntime, AgentTool, agent, agent_tool
from pyfly.agentic.agents.base import is_deferred
from pyfly.agentic.agents.registry import AgentRegistry
from pyfly.agentic.content.chunking import TextChunker
from pyfly.agentic.embeddings.providers.ollama import OllamaEmbedder
from pyfly.agentic.exceptions import RateLimitError
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.agentic.model_utils import get_model_identifier
from pyfly.agentic.models.factory import ModelFactory
from pyfly.agentic.models.spec import ModelSpec
from pyfly.agentic.pipeline.builder import PipelineBuilder
from pyfly.agentic.pipeline.context import PipelineContext
from pyfly.agentic.pipeline.steps import AgentStep
from pyfly.agentic.reasoning.chain_of_thought import ChainOfThoughtPattern
from pyfly.agentic.tools.base import ParameterSpec
from pyfly.agentic.vectorstores.memory_store import InMemoryVectorStore
from pyfly.agentic.vectorstores.types import VectorDocument
from pyfly.agentic.workflows import FileJournalBackend, Journal, ManagedAgentRunner, workflow
from pyfly.agentic.workflows import agent as workflow_agent
from pyfly.container import bean, configuration, service
from pyfly.context import ApplicationContext
from pyfly.core.config import Config

pytestmark = [pytest.mark.ollama, pytest.mark.asyncio]

SETTINGS = {"temperature": 0, "max_tokens": 512, "timeout": 45}


def _bounded(test):
    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            return await test(*args, **kwargs)

    return run


@pytest.fixture(scope="session")
def local_ollama():
    base = os.environ.get("PYFLY_OLLAMA_TEST_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("PYFLY_OLLAMA_TEST_MODEL", "qwen2.5:3b")
    embedding = os.environ.get("PYFLY_OLLAMA_TEST_EMBEDDING_MODEL", "nomic-embed-text")
    try:
        with urlopen(f"{base}/api/tags", timeout=10) as response:
            tags = json.load(response)["models"]
        with urlopen(f"{base}/api/version", timeout=10) as response:
            version = json.load(response)["version"]
    except Exception as exc:
        pytest.fail(f"Explicit Ollama acceptance requires a reachable local server at {base}: {exc}")
    installed = {item["name"] for item in tags}
    for required in (model, embedding):
        assert required in installed or f"{required}:latest" in installed, (
            f"Required Ollama model {required!r} is absent; installed: {sorted(installed)}"
        )
    return {"base": base, "model": model, "embedding": embedding, "version": version, "tags": tags}


@pytest.fixture
async def native(local_ollama):
    runtime = AgenticRuntime(AgenticConfig(default_model=f"ollama:{local_ollama['model']}"))
    model = await ModelFactory().build(
        ModelSpec(provider="ollama", model=local_ollama["model"], base_url=f"{local_ollama['base']}/v1")
    )
    runtime.own(model)
    try:
        yield runtime, model
    finally:
        await runtime.aclose()


@_bounded
async def test_application_context_configuration_factory_injection_and_shutdown(local_ollama):
    model = await ModelFactory().build(
        ModelSpec(provider="ollama", model=local_ollama["model"], base_url=f"{local_ollama['base']}/v1")
    )
    registry = AgentRegistry()

    @configuration
    class LocalConfiguration:
        @bean
        def local_registry(self) -> AgentRegistry:
            return registry

    @service
    class FictionalDesk:
        def __init__(self, factory: AgentFactory):
            self.factory = factory

        async def answer(self):
            assistant = self.factory.create("fictional-desk", model=model, model_settings=SETTINGS)
            return assistant, await assistant.run("Reply with exactly: ORCHID")

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "default-model": f"ollama:{local_ollama['model']}",
                        "plugin-auto-discover": False,
                        "default-chunk-size": 731,
                    }
                }
            }
        )
    )
    ctx.register_bean(LocalConfiguration)
    ctx.register_bean(FictionalDesk)
    async with asyncio.timeout(90):
        await ctx.start()
        runtime = ctx.get_bean(AgenticRuntime)
        runtime.own(model)
        try:
            desk = ctx.get_bean(FictionalDesk)
            assert desk.factory is ctx.get_bean(AgentFactory)
            assert desk.factory.runtime is runtime
            assert runtime.agent_registry is ctx.get_bean(AgentRegistry) is registry
            assert runtime.config.default_chunk_size == 731
            assistant, result = await desk.answer()
            assert isinstance(assistant, Agent)
            assert registry.get("fictional-desk") is assistant
            assert "ORCHID" in result.output
            assert runtime.usage_tracker.get_summary().total_tokens > 0
        finally:
            await ctx.stop()
    with pytest.raises(RuntimeError, match="closed"), runtime.activate():
        pass


@_bounded
async def test_agent_tool_and_decorated_tool_real_round_trip(native):
    runtime, model = native
    calls = []

    class Add(AgentTool):
        def __init__(self):
            super().__init__(
                "fictional_add",
                description="Add two integers.",
                parameters=[ParameterSpec(name="a", python_type=int), ParameterSpec(name="b", python_type=int)],
            )

        async def _execute(self, **kwargs: Any):
            calls.append(("add", kwargs))
            return kwargs["a"] + kwargs["b"]

    @agent_tool("fictional_label", auto_register=False)
    async def label(code: str) -> str:
        """Look up the label for a fictional record code."""
        calls.append(("label", {"code": code}))
        return "ORCHID-731"

    assistant = runtime.agent_factory.create(
        "tools",
        model=model,
        tools=[Add(), label],
        model_settings=SETTINGS,
        instructions="Always call the requested tool exactly once and repeat its result.",
    )
    result = await assistant.run("Use fictional_add with a=17 and b=25. State the result.")
    assert calls == [("add", {"a": 17, "b": 25})]
    assert "42" in result.output
    result = await assistant.run("Use fictional_label with code=fixture-9. Repeat its result exactly.")
    assert calls == [("add", {"a": 17, "b": 25}), ("label", {"code": "fixture-9"})]
    assert "ORCHID-731" in result.output


@_bounded
async def test_native_agent_decorator_and_pydantic_output(native):
    runtime, model = native

    class Person(BaseModel):
        name: str
        age: int

    with runtime.activate():

        @agent("structured", model=model, output_type=Person, model_settings=SETTINGS)
        def instructions(ctx):
            return "Extract the fictional person's name and age exactly."

    result = await instructions.run("Fictional person: Zara is 31 years old.")
    assert isinstance(result.output, Person)
    assert result.output == Person(name="Zara", age=31)
    assert runtime.agent_registry.get("structured") is instructions


@_bounded
async def test_real_streaming_and_sync_worker_bridge(native):
    runtime, model = native
    assistant = runtime.agent_factory.create("stream-sync", model=model, model_settings=SETTINGS)
    chunks = []
    async with await assistant.run_stream("Reply with exactly: ORCHID STREAM COMPLETE") as stream:
        async for delta in stream.stream_text(delta=True):
            chunks.append(delta)
    assert chunks
    assert "ORCHID STREAM COMPLETE" in "".join(chunks)
    first = await asyncio.to_thread(assistant.run_sync, "Reply with exactly: SYNC ONE")
    worker = runtime.sync_worker.loop
    second = await asyncio.to_thread(assistant.run_sync, "Reply with exactly: SYNC TWO")
    assert "SYNC ONE" in first.output and "SYNC TWO" in second.output
    assert runtime.sync_worker.loop is worker
    assert not runtime._streams and not runtime._tasks
    await runtime.aclose()
    assert worker.is_closed()


@_bounded
async def test_memory_multi_turn_and_conversation_isolation(native):
    runtime, model = native
    memory = MemoryManager()
    assistant = runtime.agent_factory.create("memory", model=model, memory=memory, model_settings=SETTINGS)
    await assistant.run("My fictional project code is ORCHID731. Remember it.", conversation_id="fixture-a")
    await assistant.run("My fictional project code is MAPLE926. Remember it.", conversation_id="fixture-b")
    a = await assistant.run("What is my project code? Reply only with the code.", conversation_id="fixture-a")
    b = await assistant.run("What is my project code? Reply only with the code.", conversation_id="fixture-b")
    assert "ORCHID731" in a.output and "MAPLE926" not in a.output
    assert "MAPLE926" in b.output and "ORCHID731" not in b.output
    assert len(memory.get_message_history("fixture-a")) >= 4
    assert len(memory.get_message_history("fixture-b")) >= 4


@_bounded
async def test_approval_pauses_harmless_append_then_resumes_once(native):
    runtime, model = native
    entries = []

    @agent_tool("append_fixture", requires_approval=True, auto_register=False)
    async def append_fixture(record_id: str) -> str:
        """Append the fictional record ID to an in-memory demonstration list."""
        entries.append(record_id)
        return f"Recorded {record_id}"

    assistant = runtime.agent_factory.create(
        "approval",
        model=model,
        tools=[append_fixture],
        model_settings=SETTINGS,
        instructions="Call append_fixture once when asked to record a fictional ID, then confirm its result.",
    )
    paused = await assistant.run("Record fictional ID fixture-42 using append_fixture.")
    assert is_deferred(paused)
    assert entries == []
    approvals = paused.output.approvals
    assert len(approvals) == 1 and approvals[0].tool_name == "append_fixture"
    assert approvals[0].args_as_dict() == {"record_id": "fixture-42"}
    resumed = await assistant.run(
        None,
        message_history=paused.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={approvals[0].tool_call_id: True}),
    )
    assert not is_deferred(resumed)
    assert entries == ["fixture-42"]
    assert "fixture-42" in resumed.output


@_bounded
async def test_dag_agent_step_uses_real_model(native):
    runtime, model = native
    assistant = runtime.agent_factory.create("dag", model=model, model_settings=SETTINGS)
    builder = PipelineBuilder()

    async def question(context, inputs):
        return "What is the capital of France? Reply with a single word."

    builder.add_node("question", question)
    builder.add_node("answer", AgentStep(assistant))
    builder.add_edge("question", "answer")
    result = await builder.build().run(PipelineContext(inputs={}))
    assert "paris" in result.outputs["answer"].output.lower()


@_bounded
async def test_workflow_dsl_real_runner_and_durable_replay(native, tmp_path):
    runtime, model = native
    assistant = runtime.agent_factory.create("workflow-agent", model=model, model_settings=SETTINGS)

    @workflow(register=False)
    async def fictional_workflow(args, ctx):
        answer = await workflow_agent("Reply with exactly: JOURNAL731", using="workflow-agent")
        return answer

    backend = FileJournalBackend(tmp_path)
    with runtime.activate():
        first = await fictional_workflow.run(
            runner=ManagedAgentRunner(registry=runtime.agent_registry),
            journal=Journal(backend=backend, run_key="fictional-731"),
        )
    assert "JOURNAL731" in first
    requests = runtime.usage_tracker.get_summary().record_count
    assert requests > 0

    class RejectModelRequests:
        async def run(self, *args, **kwargs):
            pytest.fail("A durable journal replay must not request another model completion")

    replay = await fictional_workflow.run(
        runner=RejectModelRequests(), journal=Journal(backend=backend, run_key="fictional-731")
    )
    assert replay == first
    assert runtime.usage_tracker.get_summary().record_count == requests
    assert runtime.agent_registry.get("workflow-agent") is assistant


@_bounded
async def test_reasoning_pattern_runs_real_structured_steps(native):
    runtime, model = native
    assistant = runtime.agent_factory.create(
        "reasoning",
        model=model,
        model_settings=SETTINGS,
        instructions="Use the final_result tool to return the structured ReasoningThought. "
        "Set content to a brief calculation summary, is_final to true, and final_answer to the answer. "
        "Never answer as plain text when an output tool is provided.",
    )
    pattern = ChainOfThoughtPattern(max_steps=3, step_timeout=45, output_mode="tool")
    result = await pattern.execute(
        assistant,
        "A fictional train travels 60 km in 1.5 hours. Its average speed in km/h? "
        "Calculate directly and give the final numeric answer immediately.",
    )
    assert result.steps_taken >= 1
    assert "40" in str(result.output)
    assert runtime.usage_tracker.get_summary().total_tokens > 0


@_bounded
async def test_real_embeddings_chunking_retrieval_namespace_and_grounded_answer(native, local_ollama):
    runtime, model = native
    content = (
        "Fictional project Orchid has launch code ORCHID731. Its launch day is Tuesday.\n\n"
        "Fictional project Maple has launch code MAPLE926. Its launch day is Friday."
    )
    chunks = TextChunker(chunk_size=24, chunk_overlap=0, strategy="paragraph").chunk(content)
    assert len(chunks) == 2
    async with OllamaEmbedder(model=local_ollama["embedding"], base_url=local_ollama["base"]) as embedder:
        embeddings = await embedder.embed([chunk.content for chunk in chunks])
        assert len(embeddings.embeddings) == 2
        assert all(len(vector) == 768 for vector in embeddings.embeddings)
        store = InMemoryVectorStore(embedder=embedder)
        await store.upsert(
            [VectorDocument(id="orchid", text=chunks[0].content, embedding=embeddings.embeddings[0])],
            namespace="fixture-orchid",
        )
        await store.upsert(
            [VectorDocument(id="maple", text=chunks[1].content, embedding=embeddings.embeddings[1])],
            namespace="fixture-maple",
        )
        query = "What is the launch code of project Orchid?"
        found = await store.search_text(query, top_k=2, namespace="fixture-orchid")
        other = await store.search_text(query, top_k=2, namespace="fixture-maple")
        assert [item.document.id for item in found] == ["orchid"]
        assert [item.document.id for item in other] == ["maple"]
        assert await store.search_text(query, namespace="absent-fixture") == []
        assistant = runtime.agent_factory.create(
            "grounded",
            model=model,
            model_settings=SETTINGS,
            instructions="Answer only from the supplied fictional context, repeating the exact launch code.",
        )
        result = await assistant.run(f"Context: {found[0].document.text}\nQuestion: {query}")
        assert "ORCHID731" in result.output and "MAPLE926" not in result.output


@_bounded
async def test_runtime_quota_rejects_second_request_after_real_completion(native):
    _, model = native
    runtime = AgenticRuntime(AgenticConfig(quota_enabled=True, quota_rate_limits={get_model_identifier(model): 1}))
    try:
        assistant = runtime.agent_factory.create("quota", model=model, model_settings=SETTINGS)
        first = await assistant.run("Reply with exactly: QUOTA731")
        assert "QUOTA731" in first.output
        records = runtime.usage_tracker.get_summary().record_count
        with pytest.raises(RateLimitError):
            await assistant.run("This second request must be rejected before inference.")
        assert records == 1
        assert runtime.usage_tracker.get_summary().record_count == records
    finally:
        await runtime.aclose()


@_bounded
async def test_shutdown_cancels_real_model_tool_run_without_leaving_work(native):
    runtime, model = native
    entered, cancelled = asyncio.Event(), asyncio.Event()

    @agent_tool("wait_for_fixture", auto_register=False)
    async def wait_for_fixture(record_id: str) -> str:
        """Wait for a fictional in-memory fixture operation to complete."""
        assert record_id == "fixture-731"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return record_id

    assistant = runtime.agent_factory.create(
        "cancel",
        model=model,
        model_settings=SETTINGS,
        tools=[wait_for_fixture],
        instructions="Call wait_for_fixture once using the provided record_id.",
    )
    running = asyncio.create_task(assistant.run("Call wait_for_fixture with record_id=fixture-731."))
    try:
        await asyncio.wait_for(entered.wait(), timeout=45)
        await runtime.aclose()
        assert running.cancelled()
        assert cancelled.is_set()
        assert not runtime._tasks and not runtime._streams
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@_bounded
async def test_real_ollama_tool_persists_encrypted_native_data_across_restart(local_ollama, tmp_path, monkeypatch):
    from sqlalchemy import select

    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import agentic_memory_table
    from pyfly.data.transaction import TransactionTemplate

    for name in list(os.environ):
        if name.startswith(("PYFLY_DATA_", "PYFLY_AGENTIC_")):
            monkeypatch.delenv(name)
    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'ollama-memory.db'}",
                        "ddl-auto": "create",
                    }
                },
                "agentic": {
                    "enabled": True,
                    "memory_backend": "relational",
                    "encryption_enabled": True,
                    "encryption_key": "fictional-native-memory-test-key!",
                    "plugin_auto_discover": False,
                    "observability_enabled": False,
                },
            }
        }
    )
    context = ApplicationContext(config)
    await context.start()
    try:
        runtime = context.get_bean(AgenticRuntime)
        memory = context.get_bean(MemoryManager)
        source = context.get_bean(DataSourceRegistry).primary
        model = await ModelFactory().build(
            ModelSpec(
                provider="ollama",
                model=local_ollama["model"],
                base_url=f"{local_ollama['base']}/v1",
            )
        )
        runtime.own(model)

        calls = []

        @agent_tool("remember_fixture", auto_register=False)
        async def remember_fixture(key: str, value: str) -> str:
            """Persist a fictional key and value in application-owned memory."""
            calls.append((key, value))
            await memory.async_set_fact(key, {"value": value, "source": "local-ollama"})
            return "stored"

        assistant = context.get_bean(AgentFactory).create(
            "native-data-fixture",
            model=model,
            tools=[remember_fixture],
            model_settings=SETTINGS,
            instructions="Call remember_fixture exactly once with the requested key and value, then confirm storage.",
        )
        result = await assistant.run("Call remember_fixture with key=fixture-color and value=blue.")
        assert result.output
        assert calls == [("fixture-color", "blue")]
        assert await memory.async_get_fact("fixture-color") == {"value": "blue", "source": "local-ollama"}
        table = agentic_memory_table()
        async with TransactionTemplate(source).transaction(read_only=True) as unit:
            row = (
                await unit.resource.execute(select(table.c.content).where(table.c.key == "fixture-color"))
            ).scalar_one()
        assert isinstance(row["content"], str)
        assert "blue" not in row["content"]
        assert "local-ollama" not in row["content"]
    finally:
        await context.stop()
    with pytest.raises(RuntimeError, match="closed"), runtime.activate():
        pass
    restarted = ApplicationContext(config)
    await restarted.start()
    try:
        restored = restarted.get_bean(MemoryManager)
        assert restored is not memory
        assert restarted.get_bean(DataSourceRegistry).primary is not source
        assert await restored.async_get_fact("fixture-color") == {"value": "blue", "source": "local-ollama"}
        await restored.working.async_clear()
        assert await restored.async_get_fact("fixture-color") is None
        async with TransactionTemplate(restarted.get_bean(DataSourceRegistry).primary).transaction(
            read_only=True
        ) as unit:
            assert (await unit.resource.execute(select(table.c.key))).all() == []
    finally:
        await restarted.stop()
