# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Mandatory native Agentic boundaries use real journals, never AgentEvents listeners."""

import asyncio

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.agentic.tools.decorators import agent_tool, retryable
from pyfly.audit import AuditEffectUncertain
from pyfly.data.transaction import current_unit_of_work
from tests.integration.test_audit_recording import SCOPE, setup
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")


def runtime(recorder):
    instance = AgenticRuntime(
        AgenticConfig(default_model="test", observability_enabled=False, plugin_auto_discover=False)
    )
    instance._bind_audit_recorder(lambda: recorder)
    return instance


async def test_real_agent_model_and_native_tool_admit_before_io_without_content(relational_backend: RelationalBackend):
    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    @agent_tool("fixture_action", auto_register=False)
    async def tool() -> str:
        assert current_unit_of_work() is None
        entries = await journal.read(SCOPE)
        assert entries[-1].event.event_type == "agentic.tool.call"
        assert entries[-1].event.outcome == "intent"
        calls.append(True)
        return "fixture private result"

    try:
        agent = app.agent_factory.create("fixture-agent", model=TestModel(call_tools=[tool.name]), tools=[tool])
        await agent.run("fixture private prompt", context=None)
        assert calls == [True]
        entries = await journal.read(SCOPE)
        for event_type in ("agentic.agent.run", "agentic.model.request", "agentic.tool.call"):
            matching = [entry.event.outcome for entry in entries if entry.event.event_type == event_type]
            assert matching and matching.count("intent") == matching.count("observed")
        assert all(entry.event.actor.subject_id == "verified" for entry in entries)
        assert "fixture private" not in b"".join(entry.to_bytes() for entry in entries).decode()
    finally:
        await app.aclose()


async def test_tool_modelretry_after_io_cannot_trigger_decorator_or_pydantic_retry(
    relational_backend: RelationalBackend,
):
    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    @retryable(max_retries=3, backoff=0)
    @agent_tool("uncertain_action", auto_register=False)
    async def tool() -> str:
        calls.append(True)
        raise ModelRetry("fixture private provider error")

    try:
        agent = app.agent_factory.create("uncertain-agent", model=TestModel(call_tools=[tool.name]), tools=[tool])
        from pyfly.resilience.retry import retry

        @retry(max_attempts=4)
        async def execute():
            return await agent.run("fixture private prompt")

        with pytest.raises(AuditEffectUncertain) as uncertain:
            await execute()
        assert uncertain.value.retryable is False
        assert calls == [True]
        entries = await journal.read(SCOPE)
        assert any(
            entry.event.event_type == "agentic.tool.call" and entry.event.outcome == "unknown" for entry in entries
        )
        assert "fixture private" not in str(uncertain.value)
    finally:
        await app.aclose()


async def test_model_retry_signal_after_provider_attempt_is_unknown_without_second_call(
    relational_backend: RelationalBackend,
):
    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    async def provider(messages, info):
        entries = await journal.read(SCOPE)
        assert entries[-1].event.event_type == "agentic.model.request"
        assert entries[-1].event.outcome == "intent"
        calls.append(True)
        raise ModelRetry("fixture private model error")

    try:
        agent = app.agent_factory.create("model-uncertain", model=FunctionModel(provider))
        with pytest.raises(AuditEffectUncertain):
            await agent.run("fixture private prompt")
        assert calls == [True]
        assert any(
            entry.event.event_type == "agentic.model.request" and entry.event.outcome == "unknown"
            for entry in await journal.read(SCOPE)
        )
    finally:
        await app.aclose()


async def test_real_sdk_http_failure_has_one_attempt_despite_sdk_default_retries(relational_backend: RelationalBackend):
    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    async def unavailable(request):
        calls.append(True)
        return httpx.Response(503, json={"error": {"message": "fixture private body"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        sdk = AsyncOpenAI(api_key="fixture-key", base_url="https://fixture.invalid/v1", http_client=client)
        assert sdk.max_retries > 0
        model = OpenAIChatModel("fixture-model", provider=OpenAIProvider(openai_client=sdk))
        try:
            agent = app.agent_factory.create("sdk-uncertain", model=model)
            with pytest.raises(AuditEffectUncertain) as uncertain:
                await agent.run("fixture private prompt")
            assert calls == [True]
            assert sdk.max_retries > 0
            assert "fixture private" not in str(uncertain.value)
            assert any(entry.event.outcome == "unknown" for entry in await journal.read(SCOPE))
        finally:
            await app.aclose()
            await sdk.close()


@pytest.mark.parametrize("broken", [False, True])
async def test_stream_completes_or_retains_unknown_without_content(relational_backend: RelationalBackend, broken: bool):
    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)

    async def chunks(messages, info):
        yield "fixture private chunk"
        if broken:
            raise ModelRetry("fixture private streaming error")
        yield " done"

    async def consume(agent):
        async with await agent.run_stream("fixture private prompt") as stream:
            return [part async for part in stream.stream_text(delta=True, debounce_by=None)]

    try:
        agent = app.agent_factory.create("streamed", model=FunctionModel(stream_function=chunks))
        if broken:
            with pytest.raises(AuditEffectUncertain):
                await consume(agent)
        else:
            assert await consume(agent)
        entries = await journal.read(SCOPE)
        model_outcomes = [entry.event.outcome for entry in entries if entry.event.event_type == "agentic.model.request"]
        assert model_outcomes == ["intent", "unknown" if broken else "observed"]
        assert "fixture private" not in b"".join(entry.to_bytes() for entry in entries).decode()
    finally:
        await app.aclose()


async def test_pipeline_node_unknown_fences_retries_and_same_command_resume(relational_backend: RelationalBackend):
    from pyfly.agentic.pipeline.dag import DAG, DAGNode
    from pyfly.agentic.pipeline.engine import PipelineEngine
    from pyfly.audit import AuditAdmissionRefused

    _, journal, recorder = await setup(relational_backend)
    calls = []

    class Step:
        async def execute(self, context, inputs):
            calls.append(True)
            entries = await journal.read(SCOPE)
            assert entries[-1].event.event_type == "agentic.pipeline.node"
            assert entries[-1].event.outcome == "intent"
            raise ModelRetry("fixture private pipeline error")

    dag = DAG("fixture-pipeline")
    dag.add_node(DAGNode(node_id="step", step=Step(), retry_max=3, backoff_factor=0))
    pipeline = PipelineEngine(dag)
    pipeline._bind_audit_recorder(lambda: recorder)
    with pytest.raises(AuditEffectUncertain):
        await pipeline.run(inputs="fixture private input", run_id="server-command")
    with pytest.raises(AuditAdmissionRefused):
        await pipeline.run(inputs="fixture private input", run_id="server-command")
    assert calls == [True]
    entries = await journal.read(SCOPE)
    assert [entry.event.outcome for entry in entries if entry.event.event_type == "agentic.pipeline.node"] == [
        "intent",
        "unknown",
    ]
    assert "fixture private" not in b"".join(entry.to_bytes() for entry in entries).decode()


async def test_shared_native_tool_in_parallel_runtime_contexts_keeps_verified_identity_separate(
    relational_backend: RelationalBackend,
):
    from pyfly.audit import AuditActor, AuditContext, AuditRecorder, AuditResource, AuditScope

    manager, journal, recorder = await setup(relational_backend)
    other_scope = AuditScope(tenant_id="other-tenant", workspace_id="workspace", project_id="project")

    class OtherResolver:
        async def resolve(self, operation):
            return AuditContext(
                scope=other_scope,
                actor=AuditActor(kind="service", subject_id="verified-other"),
                resource=AuditResource(kind="fixture", resource_id="other-resource"),
            )

    other_recorder = AuditRecorder(
        journal, OtherResolver(), transaction_manager=manager, modules=frozenset({"agentic"})
    )
    first, second = runtime(recorder), runtime(other_recorder)

    @agent_tool("shared_action", auto_register=False)
    async def shared() -> str:
        await asyncio.sleep(0)
        return "fixture private shared result"

    try:
        agents = [
            app.agent_factory.create(f"agent-{index}", model=TestModel(), tools=[shared])
            for index, app in enumerate((first, second))
        ]
        await asyncio.gather(*(agent.run("fixture private prompt") for agent in agents))
        assert {entry.event.actor.subject_id for entry in await journal.read(SCOPE)} == {"verified"}
        assert {entry.event.actor.subject_id for entry in await journal.read(other_scope)} == {"verified-other"}
        assert any(entry.event.event_type == "agentic.tool.call" for entry in await journal.read(other_scope))
        assert not hasattr(shared, "_pyfly_audit_factory")
    finally:
        await first.aclose()
        await second.aclose()


async def test_actual_native_di_agent_enrollment_records_model_and_tool(tmp_path):
    from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
    from pyfly.audit import AuditJournal, AuditScope
    from pyfly.audit.auto_configuration import AuditAutoConfiguration
    from pyfly.audit.recording_auto_configuration import AuditRecordingAutoConfiguration
    from pyfly.context import ApplicationContext
    from pyfly.core import Config
    from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
    from tests.audit.test_recording_configuration import HostResolver

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "audit": {"enabled": True, "recording": {"enabled": True, "modules": ["agentic"]}},
                    "agentic": {
                        "enabled": True,
                        "default-model": "test",
                        "plugin-auto-discover": False,
                        "observability-enabled": False,
                    },
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                            "ddl-auto": "create",
                        }
                    },
                }
            }
        ),
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AuditRecordingAutoConfiguration,
            AgenticAutoConfiguration,
        ],
    )
    context.register_bean(HostResolver)
    await context.start()
    try:
        app = context.get_bean(AgenticRuntime)

        @agent_tool("di_action", auto_register=False)
        async def tool() -> str:
            return "fixture private result"

        await app.agent_factory.create("native-di", model=TestModel(), tools=[tool]).run("fixture private prompt")
        entries = await context.get_bean(AuditJournal).read(
            AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project")
        )
        assert {entry.event.event_type for entry in entries} >= {
            "agentic.agent.run",
            "agentic.model.request",
            "agentic.tool.call",
        }
        assert {entry.event.actor.subject_id for entry in entries} == {"verified-host"}
    finally:
        await context.stop()


async def test_known_guard_refusal_and_unsupported_adapter_do_not_admit_tool_or_model_io(
    relational_backend: RelationalBackend,
):
    from pydantic_ai.models.wrapper import WrapperModel

    from pyfly.agentic.exceptions import ToolGuardError
    from pyfly.agentic.tools.base import GuardResult
    from pyfly.audit import AuditError

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    class Deny:
        async def check(self, tool_name, kwargs):
            return GuardResult(passed=False, reason="fixture known refusal")

    @agent_tool("guarded", guards=[Deny()], auto_register=False)
    async def tool() -> str:
        calls.append(True)
        return "unreachable"

    try:
        with app.activate(), pytest.raises(ToolGuardError):
            await tool.execute()
        assert not await journal.read(SCOPE)
        agent = app.agent_factory.create("unsupported", model=WrapperModel(TestModel()))
        with pytest.raises(AuditError, match="supported per-attempt"):
            await agent.run("fixture private prompt")
        assert not await journal.read(SCOPE)
        assert calls == []
    finally:
        await app.aclose()


@pytest.mark.parametrize("kind", ["function", "tool", "toolset", "late_registration"])
async def test_unsupported_raw_sdk_tool_is_refused_before_any_model_or_tool_io(
    relational_backend: RelationalBackend,
    kind: str,
):
    from pydantic_ai import Tool
    from pydantic_ai.toolsets import FunctionToolset

    from pyfly.audit import AuditError

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    async def raw() -> str:
        calls.append("tool")
        raise ModelRetry("fixture private retry")

    async def provider(messages, info):
        calls.append("model")
        pytest.fail("Unsupported tool configuration must fail before the model request")

    options = {"tools": [raw]} if kind == "function" else {"tools": [Tool(raw)]} if kind == "tool" else {}
    if kind == "toolset":
        options["toolsets"] = [FunctionToolset([raw])]
    try:
        agent = app.agent_factory.create("raw-tool", model=FunctionModel(provider), **options)
        if kind == "late_registration":
            agent.agent.tool_plain(raw)
        with pytest.raises(AuditError, match="AgentTool"):
            await agent.run("fixture private prompt")
        assert calls == []
        assert not await journal.read(SCOPE)
    finally:
        await app.aclose()


async def test_sdk_recovery_cannot_hide_unknown_from_outer_native_retry(relational_backend: RelationalBackend):
    from pydantic_ai.capabilities import AbstractCapability

    from pyfly.resilience.retry import retry

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []
    recoveries = []

    class Recovery(AbstractCapability):
        async def wrap_run(self, ctx, *, handler):
            try:
                return await handler()
            except Exception:
                recoveries.append(True)
                return object()

    async def provider(messages, info):
        calls.append(True)
        raise RuntimeError("fixture private provider error")

    try:
        agent = app.agent_factory.create("recovery", model=FunctionModel(provider), capabilities=[Recovery()])

        @retry(max_attempts=4)
        async def execute():
            return await agent.run("fixture private prompt")

        with pytest.raises(AuditEffectUncertain):
            await execute()
        assert calls == [True]
        assert recoveries == [True]
        assert any(entry.event.outcome == "unknown" for entry in await journal.read(SCOPE))
    finally:
        await app.aclose()


async def test_native_run_command_identity_cannot_be_readmitted(relational_backend: RelationalBackend):
    from pydantic_ai.messages import ModelResponse, TextPart

    from pyfly.audit import AuditAdmissionRefused

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    async def provider(messages, info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("fixture private answer")])

    try:
        agent = app.agent_factory.create("stable-run", model=FunctionModel(provider))
        await agent.run("fixture private prompt", run_id="native-server-command")
        with pytest.raises(AuditAdmissionRefused):
            await agent.run("fixture private prompt", run_id="native-server-command")
        assert calls == [True]
    finally:
        await app.aclose()


async def test_mandatory_agent_audit_refuses_sync_and_foreign_loop_before_io(relational_backend: RelationalBackend):
    from pyfly.audit import AuditError

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    async def provider(messages, info):
        calls.append(True)
        pytest.fail("Unsupported event-loop bridge must refuse before provider dispatch")

    await app.start()
    try:
        agent = app.agent_factory.create("loop-bound", model=FunctionModel(provider))
        with pytest.raises(AuditError, match="async"):
            await asyncio.to_thread(agent.run_sync, "fixture private prompt")
        with pytest.raises(AuditError, match="owning"):
            await asyncio.to_thread(lambda: asyncio.run(agent.run("fixture private prompt")))
        assert calls == []
        assert not await journal.read(SCOPE)
    finally:
        await app.aclose()


async def test_generic_retry_inside_native_tool_cannot_repeat_admitted_effect(relational_backend: RelationalBackend):
    from pyfly.resilience.retry import retry

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    @agent_tool("inner_native_retry", auto_register=False)
    @retry(max_attempts=3)
    async def tool() -> str:
        calls.append(True)
        raise ModelRetry("fixture private provider error")

    try:
        agent = app.agent_factory.create("inner-retry", model=TestModel(), tools=[tool])
        with pytest.raises(AuditEffectUncertain):
            await agent.run("fixture private prompt")
        assert calls == [True]
        assert any(entry.event.outcome == "unknown" for entry in await journal.read(SCOPE))
    finally:
        await app.aclose()


@pytest.mark.parametrize("positional", [False, True])
async def test_legacy_checkpoint_resume_requires_reconciliation_for_keyword_and_positional_none(
    relational_backend: RelationalBackend,
    tmp_path,
    positional: bool,
):
    from pyfly.agentic.pipeline.checkpoint import FileCheckpointer
    from pyfly.agentic.pipeline.dag import DAG, DAGEdge, DAGNode
    from pyfly.agentic.pipeline.engine import PipelineEngine
    from pyfly.audit import AuditError

    _, journal, recorder = await setup(relational_backend)
    calls = []

    class First:
        async def execute(self, context, inputs):
            return "legacy fixture state"

    class Pending:
        async def execute(self, context, inputs):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("fixture legacy interruption")
            return "unapproved replay"

    dag = DAG("legacy-fixture")
    dag.add_node(DAGNode(node_id="first", step=First()))
    dag.add_node(DAGNode(node_id="pending", step=Pending()))
    dag.add_edge(DAGEdge(source="first", target="pending"))
    pipeline = PipelineEngine(dag, checkpointer=FileCheckpointer(tmp_path / "checkpoints"))
    legacy = await pipeline.run(inputs="fixture legacy input")
    assert not legacy.success and calls == [True]
    pipeline._bind_audit_recorder(lambda: recorder)
    with pytest.raises(AuditError, match="reconciliation"):
        if positional:
            await pipeline.run(None, run_id=legacy.run_id)
        else:
            await pipeline.run(context=None, run_id=legacy.run_id)
    assert calls == [True]
    assert not await journal.read(SCOPE)


@pytest.mark.parametrize(
    "override",
    ["execute", "execute_with_ctx", "_guarded_execute", "_execute_with_listeners", "pydantic_handler", "late_instance"],
)
async def test_unsupported_native_tool_execution_override_is_refused_before_model_io(
    relational_backend: RelationalBackend,
    override: str,
):
    from types import MethodType

    from pyfly.agentic.tools.base import AgentTool
    from pyfly.audit import AuditError

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    class Custom(AgentTool):
        async def _execute(self, **kwargs):
            calls.append("tool")
            raise ModelRetry("fixture private tool error")

    async def bypass(self, **kwargs):
        calls.append("tool")
        raise ModelRetry("fixture private tool error")

    def raw_handler(self):
        async def call():
            return await bypass(self)

        return call

    if override != "late_instance":
        setattr(Custom, override, raw_handler if override == "pydantic_handler" else bypass)
    tool = Custom("unsupported")

    async def provider(messages, info):
        calls.append("model")
        pytest.fail("An unsupported native execution override must fail before model I/O")

    try:
        agent = app.agent_factory.create("override", model=FunctionModel(provider), tools=[tool])
        if override == "late_instance":
            tool.execute = MethodType(bypass, tool)
        with pytest.raises(AuditError, match="boundary overrides"):
            await agent.run("fixture private prompt")
        assert calls == []
        assert not await journal.read(SCOPE)
    finally:
        await app.aclose()


async def test_native_tool_execute_extension_remains_supported(relational_backend: RelationalBackend):
    from pyfly.agentic.tools.base import AgentTool

    _, journal, recorder = await setup(relational_backend)
    app = runtime(recorder)
    calls = []

    class Native(AgentTool):
        async def _execute(self, **kwargs):
            calls.append(True)
            return "fixture private result"

    try:
        await app.agent_factory.create("native-subclass", model=TestModel(), tools=[Native("native")]).run("fixture")
        assert calls == [True]
        assert [
            entry.event.outcome for entry in await journal.read(SCOPE) if entry.event.event_type == "agentic.tool.call"
        ] == ["intent", "observed"]
    finally:
        await app.aclose()
