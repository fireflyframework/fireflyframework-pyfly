"""Application ownership contracts for the native Agentic module."""

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from pyfly.agentic.agents.registry import agent_registry
from pyfly.agentic.config import AgenticConfig, get_config
from pyfly.agentic.observability.usage import UsageRecord, default_usage_tracker
from pyfly.agentic.runtime import AgenticRuntime


@pytest.mark.asyncio
async def test_two_runtimes_keep_nested_defaults_and_ledgers_isolated():
    a = AgenticRuntime(AgenticConfig(default_model="test", budget_limit_usd=1))
    b = AgenticRuntime(AgenticConfig(default_model="test", budget_limit_usd=2))

    async def exercise(runtime, label):
        with runtime.activate():
            await asyncio.sleep(0)
            assert get_config() is runtime.config
            default_usage_tracker.record(UsageRecord(agent=label, cost_usd=0.1))
            agent = runtime.agent_factory.create(label, model=TestModel())
            assert agent_registry.get(label) is agent
            assert not agent_registry.has("b" if label == "a" else "a")

    await asyncio.gather(exercise(a, "a"), exercise(b, "b"))
    assert a.usage_tracker.records[0].agent == "a"
    assert b.usage_tracker.records[0].agent == "b"
    await a.aclose()
    with b.activate():
        assert agent_registry.has("b")
    await b.aclose()


@pytest.mark.asyncio
async def test_bound_agent_selects_its_runtime_for_tool_execution():
    from pyfly.agentic.agents.base import Agent

    runtime = AgenticRuntime(AgenticConfig(default_model="test", default_chunk_size=999))
    seen = []

    def check() -> str:
        seen.append(get_config().default_chunk_size)
        return "ok"

    agent = Agent("bound", model=TestModel(call_tools=["check"]), tools=[check], runtime=runtime)
    await agent.run("hello")
    assert seen == [999]
    assert len(runtime.usage_tracker.records) == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_resource_ownership_close_once_and_external_preserved():
    class Resource:
        calls = 0

        async def aclose(self):
            self.calls += 1

    owned, external = Resource(), Resource()
    runtime = AgenticRuntime(AgenticConfig())
    runtime.own(owned)
    runtime.own(owned)
    runtime.external(external)
    await runtime.aclose()
    await runtime.aclose()
    assert owned.calls == 1
    assert external.calls == 0


def test_native_config_uses_framework_binding_without_dotenv(tmp_path, monkeypatch):
    from pyfly.agentic.auto_configuration import bind_agentic_config
    from pyfly.core.config import Config

    (tmp_path / ".env").write_text("PYFLY_AGENTIC_DEFAULT_MODEL=wrong\n")
    monkeypatch.chdir(tmp_path)
    assert AgenticConfig().default_model != "wrong"
    monkeypatch.setenv("PYFLY_AGENTIC_USAGE_TRACKER_MAX_RECORDS", "7")
    monkeypatch.setenv("PYFLY_AGENTIC_QUOTA_RATE_LIMITS", '{"test": 2}')
    cfg = bind_agentic_config(Config({"pyfly": {"agentic": {"default-model": "test"}}}))
    assert cfg.default_model == "test"
    assert cfg.usage_tracker_max_records == 7
    assert cfg.quota_rate_limits == {"test": 2}


@pytest.mark.asyncio
async def test_actual_application_context_exposes_runtime_factory_and_override():
    from pyfly.agentic.agents.registry import AgentRegistry
    from pyfly.agentic.factory import AgentFactory
    from pyfly.container import bean, configuration
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    registry = AgentRegistry()

    @configuration
    class UserConfig:
        @bean
        def custom_agents(self) -> AgentRegistry:
            return registry

    ctx = ApplicationContext(
        Config({"pyfly": {"agentic": {"enabled": True, "default-model": "test", "plugin-auto-discover": False}}})
    )
    ctx.register_bean(UserConfig)
    await ctx.start()
    try:
        runtime = ctx.get_bean(AgenticRuntime)
        factory = ctx.get_bean(AgentFactory)
        agent = factory.create("context-agent", model=TestModel())
        assert runtime.agent_registry is registry
        assert ctx.get_bean(AgentRegistry) is registry
        assert registry.get("context-agent") is agent
        assert (await agent.run("hello")).output
    finally:
        await ctx.stop()
    assert runtime._closed


@pytest.mark.asyncio
async def test_bound_stream_selects_runtime_through_iteration_and_cleanup():
    runtime = AgenticRuntime(AgenticConfig(default_model="test", default_chunk_size=989))
    agent = runtime.agent_factory.create("stream", model=TestModel())
    manager = await agent.run_stream("hello")
    async with manager as stream:
        assert get_config() is runtime.config
        async for _ in stream.stream_text():
            assert get_config().default_chunk_size == 989
            break
    assert not runtime._streams
    assert not runtime._tasks
    await runtime.aclose()


@pytest.mark.asyncio
async def test_sync_workers_are_persistent_owned_and_independent():
    a = AgenticRuntime(AgenticConfig(default_model="test"))
    b = AgenticRuntime(AgenticConfig(default_model="test"))
    one = a.agent_factory.create("one", model=TestModel())
    two = b.agent_factory.create("two", model=TestModel())
    one.run_sync("hello")
    first = a.sync_worker.loop
    one.run_sync("again")
    assert a.sync_worker.loop is first
    two.run_sync("hello")
    assert b.sync_worker.loop is not first
    await a.aclose()
    assert first.is_closed()
    assert two.run_sync("still running").output
    await b.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_run_and_resets_context():
    from pydantic_ai.models.function import FunctionModel

    entered = asyncio.Event()

    async def blocking(messages, info):
        entered.set()
        await asyncio.Event().wait()

    runtime = AgenticRuntime(AgenticConfig(default_model="test"))
    agent = runtime.agent_factory.create("blocking", model=FunctionModel(blocking))
    task = asyncio.create_task(agent.run("hello"))
    await entered.wait()
    await runtime.aclose()
    assert task.cancelled()
    assert get_config() is not runtime.config


@pytest.mark.asyncio
async def test_runtime_rate_limit_and_daily_budget_are_application_owned():
    from pyfly.agentic.exceptions import BudgetExceededError, RateLimitError

    a = AgenticRuntime(
        AgenticConfig(
            default_model="test", quota_enabled=True, quota_rate_limits={"test": 1}, quota_budget_daily_usd=0.5
        )
    )
    b = AgenticRuntime(AgenticConfig(default_model="test", quota_enabled=True, quota_rate_limits={"test": 1}))
    first = a.agent_factory.create("limited", model=TestModel())
    second = b.agent_factory.create("independent", model=TestModel())
    await first.run("one")
    with pytest.raises(RateLimitError):
        await first.run("two")
    await second.run("allowed")
    with pytest.raises(BudgetExceededError):
        a.usage_tracker.record(UsageRecord(cost_usd=0.6))
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio
async def test_registered_workflow_carries_runtime_into_nested_calls():
    from pyfly.agentic.workflows import agent, workflow

    runtime = AgenticRuntime(AgenticConfig(default_model="test", default_chunk_size=987))
    with runtime.activate():

        @workflow(name="native-workflow")
        async def native_workflow(args):
            assert get_config().default_chunk_size == 987
            return await agent("hello", model=TestModel())

    result = await native_workflow.run()
    assert result
    assert len(runtime.usage_tracker.records) == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_database_store_keeps_borrowed_application_loop_outside_activation():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.agentic.memory.relational_store import RelationalMemoryStore

    engine = create_async_engine("sqlite+aiosqlite://")
    store = RelationalMemoryStore(engine, create_table=True)
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), memory_store=store)
    try:
        await runtime.start()
        await runtime.memory.async_set_fact("answer", {"value": 42})
        assert await runtime.memory.async_get_fact("answer") == {"value": 42}
        assert runtime.database_worker.loop is None
        await runtime.aclose()
        assert store._initialized
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await runtime.aclose()
        await store.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_database_start_failure_preserves_borrowed_store_and_can_restart(monkeypatch):
    from unittest.mock import AsyncMock

    from pyfly.agentic.memory.store import InMemoryStore

    store = InMemoryStore()
    initialize = AsyncMock(side_effect=ValueError("schema failed"))
    close = AsyncMock()
    monkeypatch.setattr(store, "initialize", initialize, raising=False)
    monkeypatch.setattr(store, "close", close, raising=False)
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), memory_store=store)
    with pytest.raises(ValueError, match="schema failed"):
        await runtime.start()
    assert runtime.database_worker.loop is None
    assert close.await_count == 0
    initialize.side_effect = None
    await runtime.start()
    await runtime.aclose()
    assert initialize.await_count == 2
    assert close.await_count == 0


@pytest.mark.asyncio
async def test_framework_private_providers_receive_native_and_agent_spans():
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    before = trace.get_tracer_provider()
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {"enabled": True, "plugin-auto-discover": False},
                    "observability": {"tracing": {"register-global": False}, "metrics": {"register-global": False}},
                }
            }
        )
    )
    await ctx.start()
    try:
        runtime = ctx.get_bean(AgenticRuntime)
        provider = ctx.get_bean(trace.TracerProvider)
        assert runtime.tracer_provider is provider
        assert provider is not before
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        agent = runtime.agent_factory.create("private", model=TestModel())
        await agent.run("hello")
        names = [span.name for span in exporter.get_finished_spans()]
        assert "agent.private" in names
        assert any("chat" in name for name in names)
        assert trace.get_tracer_provider() is before
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_async_runtime_context_selects_defaults_and_restores_them():
    before = get_config()
    runtime = AgenticRuntime(AgenticConfig(default_model="test", plugin_auto_discover=False))
    async with runtime:
        assert get_config() is runtime.config
    assert get_config() is before


@pytest.mark.asyncio
async def test_cancelled_run_closes_its_observability_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai.models.function import FunctionModel

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    entered = asyncio.Event()

    async def blocking(messages, info):
        entered.set()
        await asyncio.Event().wait()

    runtime = AgenticRuntime(AgenticConfig(), tracer_provider=provider)
    agent = runtime.agent_factory.create("cancel-span", model=FunctionModel(blocking))
    task = asyncio.create_task(agent.run("hello"))
    await entered.wait()
    await runtime.aclose()
    assert task.cancelled()
    assert "agent.cancel-span" in [s.name for s in exporter.get_finished_spans()]
    provider.shutdown()


def test_disabled_entrypoint_has_no_sdk_configuration_or_runtime_import():
    import subprocess
    import sys

    script = """import sys, importlib.abc
class NoAI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("pydantic_ai", "pydantic_graph")):
            raise AssertionError("AI import during disabled discovery: " + fullname)
sys.meta_path.insert(0, NoAI())
from pyfly.agentic.auto_configuration import AgenticAutoConfiguration
assert "pyfly.agentic.config" not in sys.modules
assert "pyfly.agentic.runtime" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_enabled_missing_dependencies_raise_useful_error(monkeypatch):
    import pyfly.agentic.auto_configuration as auto

    monkeypatch.setattr(auto.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match=r"pyfly\[agentic\].*pydantic_ai"):
        auto.AgenticAutoConfiguration()


@pytest.mark.asyncio
async def test_transient_agent_beans_are_not_retained_by_application_registry():
    from pyfly.agentic.agents.base import Agent
    from pyfly.agentic.factory import AgentFactory
    from pyfly.container import Scope, bean, configuration
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    @configuration
    class Beans:
        @bean(scope=Scope.TRANSIENT)
        def temporary_agent(self, factory: AgentFactory) -> Agent:
            return factory.create("temporary", model=TestModel())

    ctx = ApplicationContext(Config({"pyfly": {"agentic": {"enabled": True, "plugin-auto-discover": False}}}))
    ctx.register_bean(Beans)
    await ctx.start()
    try:
        agent = ctx.get_bean(Agent)
        runtime = ctx.get_bean(AgenticRuntime)
        assert not runtime.agent_registry.has("temporary")
        assert agent._runtime is runtime
        assert (await agent.run("hello")).output
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_two_application_contexts_isolate_models_quota_memory_and_providers():
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    def context(size):
        return ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "agentic": {
                            "enabled": True,
                            "default-model": "test",
                            "default-chunk-size": size,
                            "plugin-auto-discover": False,
                            "quota-enabled": True,
                            "quota-rate-limits": {"test": 2},
                            "budget-limit-usd": size / 1000,
                        },
                        "observability": {"tracing": {"register-global": False}, "metrics": {"register-global": False}},
                    }
                }
            )
        )

    a, b = context(901), context(902)
    await asyncio.gather(a.start(), b.start())
    try:
        one, two = a.get_bean(AgenticRuntime), b.get_bean(AgenticRuntime)
        assert one.tracer_provider is not two.tracer_provider
        assert one.meter_provider is not two.meter_provider

        def check() -> str:
            return str(get_config().default_chunk_size)

        first = one.agent_factory.create("same-name", model=TestModel(call_tools=["check"]), tools=[check])
        second = two.agent_factory.create("same-name", model=TestModel(call_tools=["check"]), tools=[check])
        results = await asyncio.gather(
            first.run("alpha", conversation_id="thread"), second.run("beta", conversation_id="thread")
        )
        assert "901" in str(results[0].output)
        assert "902" in str(results[1].output)
        assert one.agent_registry.get("same-name") is first
        assert two.agent_registry.get("same-name") is second
        assert "alpha" in str(one.memory.get_message_history("thread"))
        assert "beta" not in str(one.memory.get_message_history("thread"))
        assert one.quota_manager.get_rate_limit_remaining("test") == 1
        await a.stop()
        assert (await second.run("still alive")).output
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_config_install_hooks_remain_owned_by_selected_application():
    from pyfly.agentic.config import on_config_installed, set_config

    a, b = AgenticRuntime(AgenticConfig()), AgenticRuntime(AgenticConfig())
    seen = []
    with a.activate():
        on_config_installed(lambda cfg: seen.append(cfg.default_model))
    with b.activate():
        set_config(AgenticConfig(default_model="test"))
    assert seen == [a.config.default_model]
    with a.activate():
        set_config(AgenticConfig(default_model="test"))
    assert seen[-1] == "test"
    await a.aclose()
    await b.aclose()


@pytest.mark.asyncio
async def test_injected_service_calls_the_registered_singleton_agent():
    from pyfly.agentic import Agent, AgentFactory
    from pyfly.container import bean, configuration, service
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    @configuration
    class Beans:
        @bean
        def assistant(self, factory: AgentFactory) -> Agent:
            return factory.create("assistant-bean", model=TestModel())

    @service
    class Assistance:
        def __init__(self, assistant: Agent):
            self.assistant = assistant

        async def answer(self, question):
            return (await self.assistant.run(question)).output

    ctx = ApplicationContext(Config({"pyfly": {"agentic": {"enabled": True, "plugin-auto-discover": False}}}))
    ctx.register_bean(Beans)
    ctx.register_bean(Assistance)
    await ctx.start()
    try:
        service_bean = ctx.get_bean(Assistance)
        runtime = ctx.get_bean(AgenticRuntime)
        assert runtime.agent_registry.get("assistant-bean") is service_bean.assistant
        assert await service_bean.answer("hello")
        assert len(runtime.usage_tracker.records) == 1
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_shutdown_drains_sync_run_before_closing_owned_resources():
    import threading

    from pydantic_ai.models.function import FunctionModel

    entered = threading.Event()
    cancelled = threading.Event()

    class Resource:
        async def aclose(self):
            assert cancelled.is_set()

    async def blocking(messages, info):
        entered.set()
        await asyncio.Event().wait()

    class Cleanup:
        async def before_run(self, context):
            pass

        async def after_run(self, context, result):
            return result

        async def on_error(self, context, error):
            await asyncio.sleep(0.02)
            cancelled.set()

    runtime = AgenticRuntime(AgenticConfig())
    runtime.own(Resource())
    agent = runtime.agent_factory.create("sync-block", model=FunctionModel(blocking), middleware=[Cleanup()])
    invocation = asyncio.create_task(asyncio.to_thread(agent.run_sync, "hello"))
    await asyncio.to_thread(entered.wait, 2)
    try:
        await runtime.aclose()
    finally:
        await asyncio.gather(invocation, return_exceptions=True)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_explicit_agent_configuration_is_selected_for_nested_tools():
    from pyfly.agentic import Agent

    runtime = AgenticRuntime(AgenticConfig(default_model="test", default_chunk_size=930))
    explicit = AgenticConfig(default_model="test", default_chunk_size=939)
    seen = []

    def inspect_config() -> str:
        seen.append(get_config().default_chunk_size)
        return "ok"

    agent = Agent(
        "override",
        runtime=runtime,
        config=explicit,
        model=TestModel(call_tools=["inspect_config"]),
        tools=[inspect_config],
    )
    await agent.run("hello")
    assert seen == [939]
    assert runtime.config.default_chunk_size == 930
    await runtime.aclose()


@pytest.mark.asyncio
async def test_framework_correlation_is_retained_in_agent_usage():
    from pyfly.observability.correlation import bind_correlation_context, unbind_correlation_context

    runtime = AgenticRuntime(AgenticConfig(default_model="test"))
    agent = runtime.agent_factory.create("correlated", model=TestModel())
    tokens = bind_correlation_context(
        correlation_id="fictional-request", request_id=None, tenant_id=None, traceparent=None, tracestate=None
    )
    try:
        await agent.run("hello")
    finally:
        unbind_correlation_context(tokens)
    assert runtime.usage_tracker.records[0].correlation_id == "fictional-request"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_concurrent_shutdown_waits_and_rejects_new_agent_work():
    entered, release = asyncio.Event(), asyncio.Event()

    class Resource:
        calls = 0

        async def aclose(self):
            self.calls += 1
            entered.set()
            await release.wait()

    runtime = AgenticRuntime(AgenticConfig(default_model="test"))
    resource = runtime.own(Resource())
    agent = runtime.agent_factory.create("shutdown", model=TestModel())
    first = asyncio.create_task(runtime.aclose())
    await entered.wait()
    second = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    try:
        assert not second.done()
        with pytest.raises(RuntimeError, match="shutting down"):
            await agent.run("too late")
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert resource.calls == 1


@pytest.mark.asyncio
async def test_plugin_factories_create_application_owned_instances(monkeypatch):
    from pyfly.agentic import Agent
    from pyfly.agentic.plugin import ENTRY_POINT_AGENTS, DiscoveredPlugin, DiscoveryResult, PluginDiscovery

    def factory():
        return Agent("plugin-agent", model=TestModel())

    monkeypatch.setattr(
        PluginDiscovery,
        "discover_all",
        lambda: DiscoveryResult(
            plugins=[
                DiscoveredPlugin(
                    group=ENTRY_POINT_AGENTS, name="fictional", value="fictional:factory", loaded_object=factory
                )
            ]
        ),
    )
    a, b = AgenticRuntime(AgenticConfig()), AgenticRuntime(AgenticConfig())
    await asyncio.gather(a.start(), b.start())
    try:
        one, two = a.agent_registry.get("plugin-agent"), b.agent_registry.get("plugin-agent")
        assert one is not two
        assert one._runtime is a
        assert two._runtime is b
    finally:
        await a.aclose()
        await b.aclose()
