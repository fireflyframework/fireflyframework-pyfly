"""Native host integration contracts for Agentic infrastructure."""

import asyncio
from types import SimpleNamespace

import pytest

from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.observability.events import AgentEvent, AgentEvents
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.agentic.tools.builtins.http import HttpTool
from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher


@pytest.mark.asyncio
async def test_native_events_await_listener_and_keep_apps_isolated():
    bus = ApplicationEventBus()
    seen = []

    async def listener(event):
        await asyncio.sleep(0)
        seen.append(event)

    bus.subscribe(AgentEvent, listener)
    events = AgentEvents(publisher=ApplicationEventPublisher(bus))
    await events.aagent_started("worker", model="test")
    await events.atool_executed("lookup")
    await events.areasoning_step("plan", 1)
    await events.aagent_completed("worker")
    await AgentEvents().aagent_started("other")
    assert [e.event_type for e in seen] == ["agent.started", "tool.executed", "reasoning.step", "agent.completed"]


@pytest.mark.asyncio
async def test_native_event_listener_failure_is_best_effort_but_cancellation_propagates():
    bus = ApplicationEventBus()

    async def failed(event):
        raise ValueError("telemetry unavailable")

    bus.subscribe(AgentEvent, failed)
    events = AgentEvents(publisher=ApplicationEventPublisher(bus))
    await events.aagent_started("worker")
    bus2 = ApplicationEventBus()

    async def cancelled(event):
        raise asyncio.CancelledError

    bus2.subscribe(AgentEvent, cancelled)
    with pytest.raises(asyncio.CancelledError):
        await AgentEvents(publisher=ApplicationEventPublisher(bus2)).aagent_started("worker")


class BorrowedHttp:
    def __init__(self):
        self.requests = []
        self.closed = 0

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return SimpleNamespace(status_code=201, headers={"content-type": "text/plain"}, text="created")

    async def start(self):
        pass

    async def stop(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_http_tool_borrows_native_port_and_runtime_default():
    client = BorrowedHttp()
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), http_client=client)
    with runtime.activate():
        tool = HttpTool(default_headers={"X-App": "native"})
    result = await tool.execute(
        url="https://example.invalid/resource", method="post", body="body", headers={"X-Run": "one"}
    )
    assert result == {"status": 201, "headers": {"content-type": "text/plain"}, "body": "created"}
    assert client.requests == [
        (
            "POST",
            "https://example.invalid/resource",
            {"content": b"body", "headers": {"X-App": "native", "X-Run": "one"}},
        )
    ]
    await tool.close()
    await runtime.aclose()
    assert client.closed == 0


@pytest.mark.asyncio
async def test_plugin_construction_injects_container_and_closes_once_on_restart(monkeypatch):
    from pyfly.agentic.plugin import ENTRY_POINT_TOOLS, DiscoveredPlugin, DiscoveryResult, PluginDiscovery
    from pyfly.agentic.tools.base import AgentTool
    from pyfly.container import Container
    from pyfly.context.lifecycle import post_construct, pre_destroy

    made = []

    class Service:
        pass

    class NativeTool(AgentTool):
        def __init__(self, service: Service):
            super().__init__("native")
            self.service = service
            self.closed = 0
            self.ready = False
            made.append(self)

        @post_construct
        async def initialize(self):
            self.ready = True

        @pre_destroy
        async def dispose(self):
            self.closed += 1

        async def _execute(self, **kwargs):
            return "native"

    # Local annotations must be resolvable by the container's normal get_type_hints path.
    NativeTool.__init__.__annotations__["service"] = Service
    container = Container()
    container.register(Service)
    monkeypatch.setattr(
        PluginDiscovery,
        "discover_all",
        lambda: DiscoveryResult([DiscoveredPlugin(ENTRY_POINT_TOOLS, "native", "test:NativeTool", NativeTool)]),
    )
    runtime = AgenticRuntime(AgenticConfig(), container=container)
    await runtime.start()
    tool = runtime.tool_registry.get("native")
    assert tool.service is container.resolve(Service)
    assert tool.ready
    await runtime.aclose()
    await runtime.aclose()
    assert tool.closed == 1
    await runtime.start()
    replacement = runtime.tool_registry.get("native")
    assert len(runtime._plugin_resources) == 1
    assert replacement is not tool
    await runtime.aclose()
    assert replacement.closed == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("experiment_storage", "sqlite:///x"),
        ("tools_sandbox_enabled", True),
        ("http_pool_size", 7),
        ("http_pool_timeout", 4),
        ("memory_postgres_url", "postgresql://localhost/x"),
        ("memory_mongodb_pool_size", 3),
    ],
)
def test_removed_inert_configuration_is_rejected_explicitly(field, value):
    with pytest.raises(ValueError, match="native|host|pyfly"):
        AgenticConfig(**{field: value})


def test_artifact_cache_root_uses_native_configuration_and_rejects_old_environment(tmp_path, monkeypatch):
    from pyfly.agentic.auto_configuration import bind_agentic_config
    from pyfly.agentic.storage.database_store import _resolve_cache_root
    from pyfly.core.config import Config

    runtime = AgenticRuntime(AgenticConfig(artifact_cache_root=str(tmp_path)))
    with runtime.activate():
        assert _resolve_cache_root(None) == tmp_path
        assert _resolve_cache_root(tmp_path / "explicit") == tmp_path / "explicit"
    monkeypatch.setenv("FIREFLY_DBSTORE_CACHE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="PYFLY_AGENTIC_ARTIFACT_CACHE_ROOT"):
        bind_agentic_config(Config())


@pytest.mark.asyncio
async def test_explicit_http_client_override_wins_and_is_not_closed():
    first, override = BorrowedHttp(), BorrowedHttp()
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), http_client=first)
    with runtime.activate():
        tool = HttpTool(client=override)
    await tool.execute(url="https://example.invalid")
    await tool.close()
    await runtime.aclose()
    assert len(override.requests) == 1
    assert not first.requests
    assert override.closed == first.closed == 0


@pytest.mark.asyncio
async def test_event_transaction_phase_outside_transaction_runs_inline():
    from pyfly.data.transaction.synchronization import TransactionPhase

    bus = ApplicationEventBus()
    received = []

    async def listener(event):
        received.append(event.agent)

    bus.subscribe(AgentEvent, listener, phase=TransactionPhase.AFTER_COMMIT)
    await AgentEvents(publisher=ApplicationEventPublisher(bus)).aagent_started("outside")
    assert received == ["outside"]


@pytest.mark.asyncio
async def test_eda_export_requires_explicit_adapter_and_destination():
    from pyfly.observability.correlation import bind_correlation_context, unbind_correlation_context

    exports = []

    class Exporter:
        async def publish(self, destination, event_type, payload, headers=None):
            exports.append((destination, event_type, payload, headers))

    with pytest.raises(ValueError, match="both"):
        AgentEvents(eda_publisher=Exporter())
    token = bind_correlation_context(
        correlation_id="native-run", request_id=None, tenant_id=None, traceparent=None, tracestate=None
    )
    try:
        events = AgentEvents(eda_publisher=Exporter(), eda_destination="agentic-audit")
        await events.aagent_completed("worker", tokens=7)
    finally:
        unbind_correlation_context(token)
    assert len(exports) == 1
    assert exports[0][0:2] == ("agentic-audit", "agent.completed")
    assert exports[0][2]["detail"]["tokens"] == 7
    assert exports[0][3]["X-Correlation-Id"] == "native-run"


@pytest.mark.asyncio
async def test_actual_context_delivers_run_and_tool_events_once_to_its_own_listener():
    from pydantic_ai.models.test import TestModel

    from pyfly.agentic.tools.base import AgentTool
    from pyfly.client.ports.outbound import HttpClientPort
    from pyfly.container import bean, configuration
    from pyfly.context import ApplicationContext
    from pyfly.context.events import app_event_listener
    from pyfly.core.config import Config

    @configuration
    class Host:
        @bean
        def http_client(self) -> HttpClientPort:
            return BorrowedHttp()

    class Listener:
        def __init__(self):
            self.events = []

        @app_event_listener
        async def observe(self, event: AgentEvent):
            await asyncio.sleep(0)
            self.events.append(event)

    class Lookup(AgentTool):
        def __init__(self):
            super().__init__("lookup")

        async def _execute(self, **kwargs):
            return "local"

    contexts = [
        ApplicationContext(
            Config(
                {"pyfly": {"agentic": {"enabled": True, "plugin-auto-discover": False, "observability-enabled": False}}}
            )
        )
        for _ in range(2)
    ]
    for ctx in contexts:
        ctx.register_bean(Host)
        ctx.register_bean(Listener)
        await ctx.start()
    try:
        runtime = contexts[0].get_bean(AgenticRuntime)
        agent = runtime.agent_factory.create(
            "native-events", model=TestModel(call_tools=["lookup"]), tools=[Lookup()], default_middleware=False
        )
        await agent.run("look up locally")
        types = [event.event_type for event in contexts[0].get_bean(Listener).events]
        assert types.count("agent.started") == types.count("agent.completed") == 1
        assert types.count("tool.executed") == 1
        assert contexts[1].get_bean(Listener).events == []
        assert contexts[0].get_bean(HttpTool)._native_client is contexts[0].get_bean(HttpClientPort)
    finally:
        for ctx in reversed(contexts):
            await ctx.stop()


@pytest.mark.asyncio
async def test_plugin_factory_injects_native_services_and_failed_init_closes_immediately(monkeypatch):
    from pyfly.agentic.plugin import ENTRY_POINT_TOOLS, DiscoveredPlugin, DiscoveryResult, PluginDiscovery
    from pyfly.agentic.tools.base import AgentTool
    from pyfly.client.ports.outbound import HttpClientPort
    from pyfly.container import Container
    from pyfly.context.lifecycle import post_construct

    created = []

    class FailedTool(AgentTool):
        def __init__(self, client):
            super().__init__("failed")
            self.client = client
            self.closed = 0
            created.append(self)

        @post_construct
        async def initialize(self):
            raise ValueError("fictional initialization failure")

        async def close(self):
            self.closed += 1

        async def _execute(self, **kwargs):
            return None

    def factory(client: HttpClientPort):
        return FailedTool(client)

    client = BorrowedHttp()
    container = Container()
    container.register_instance(HttpClientPort, client)
    monkeypatch.setattr(
        PluginDiscovery,
        "discover_all",
        lambda: DiscoveryResult([DiscoveredPlugin(ENTRY_POINT_TOOLS, "failed", "test:factory", factory)]),
    )
    runtime = AgenticRuntime(AgenticConfig(), container=container)
    await runtime.start()
    assert len(runtime.discovery.failed) == 1
    assert not runtime.tool_registry.has("failed")
    assert created[0].client is client
    assert created[0].closed == 1
    await runtime.aclose()
    assert created[0].closed == 1
    assert client.closed == 0


@pytest.mark.asyncio
async def test_native_relational_context_binds_named_datasource_and_keeps_resources_borrowed(tmp_path):
    from sqlalchemy import text

    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    config = Config(
        {
            "pyfly": {
                "agentic": {
                    "enabled": True,
                    "plugin-auto-discover": False,
                    "memory-backend": "relational",
                    "memory-datasource": "agent-memory",
                },
                "data": {
                    "relational": {
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}",
                        "ddl-auto": "create",
                        "datasources": {"agent-memory": {"url": f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"}},
                    }
                },
            }
        }
    )
    ctx = ApplicationContext(config)
    await ctx.start()
    try:
        runtime = ctx.get_bean(AgenticRuntime)
        registry = ctx.get_bean(DataSourceRegistry)
        assert runtime.memory.store.engine is registry.get("agent-memory").engine
        await runtime.memory.async_set_fact("native", {"value": 7})
        assert await runtime.memory.async_get_fact("native") == {"value": 7}
        from pyfly.data.transaction import TransactionTemplate
        from pyfly.data.transaction.synchronization import TransactionPhase

        committed = []

        async def after_commit(event):
            committed.append(event.agent)

        runtime.events._publisher._bus.subscribe(AgentEvent, after_commit, phase=TransactionPhase.AFTER_COMMIT)
        async with TransactionTemplate(registry.get("agent-memory")).transaction():
            await runtime.events.aagent_completed("committed")
            assert committed == []
        assert committed == ["committed"]
        with pytest.raises(ValueError, match="rollback"):
            async with TransactionTemplate(registry.get("agent-memory")).transaction():
                await runtime.events.aagent_completed("rolled-back")
                raise ValueError("rollback")
        assert committed == ["committed"]
        await runtime.aclose()
        async with registry.get("agent-memory").engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        await runtime.start()
        assert await runtime.memory.async_get_fact("native") == {"value": 7}
    finally:
        await ctx.stop()


@pytest.mark.parametrize("origin", ["yaml", "environment"])
@pytest.mark.parametrize(
    "field", ["experiment_storage", "tools_sandbox_enabled", "http_pool_timeout", "memory_postgres_pool_size"]
)
def test_removed_configuration_rejected_from_host_tree_and_environment(monkeypatch, origin, field):
    from pyfly.agentic.auto_configuration import bind_agentic_config
    from pyfly.core.config import Config

    config = Config({"pyfly": {"agentic": {field.replace("_", "-"): "legacy"}}}) if origin == "yaml" else Config()
    if origin == "environment":
        monkeypatch.setenv("PYFLY_AGENTIC_" + field.upper(), "legacy")
    with pytest.raises(ValueError, match="removed"):
        bind_agentic_config(config)


@pytest.mark.asyncio
async def test_plugin_start_failure_stops_resource_and_does_not_abort_other_plugins(monkeypatch):
    from pyfly.agentic.plugin import ENTRY_POINT_TOOLS, DiscoveredPlugin, DiscoveryResult, PluginDiscovery
    from pyfly.agentic.tools.base import AgentTool

    instances = []

    class LifecycleTool(AgentTool):
        def __init__(self):
            super().__init__("lifecycle")
            self.stopped = 0
            instances.append(self)

        async def start(self):
            raise ValueError("start failed")

        async def stop(self):
            self.stopped += 1

        async def _execute(self, **kwargs):
            return "local"

    monkeypatch.setattr(
        PluginDiscovery,
        "discover_all",
        lambda: DiscoveryResult(
            [DiscoveredPlugin(ENTRY_POINT_TOOLS, "lifecycle", "test:LifecycleTool", LifecycleTool)]
        ),
    )
    runtime = AgenticRuntime(AgenticConfig())
    await runtime.start()
    assert len(runtime.discovery.failed) == 1
    assert instances[0].stopped == 1
    await runtime.aclose()
    assert instances[0].stopped == 1


@pytest.mark.asyncio
async def test_content_and_quality_defaults_observe_selected_runtime_and_explicit_overrides():
    from pyfly.agentic.content.chunking import TextChunker
    from pyfly.agentic.content.compression import MapReduceStrategy, SlidingWindowManager
    from pyfly.agentic.validation.qos import ConsistencyChecker, QoSGuard

    calls = []

    class Agent:
        async def run(self, prompt, **kwargs):
            calls.append(prompt)
            return SimpleNamespace(output="0.75")

    class Scorer:
        async def score(self, output):
            return 0.75

    cfg = AgenticConfig(
        default_chunk_size=4,
        default_chunk_overlap=0,
        max_context_tokens=4,
        qos_min_confidence=0.9,
        qos_consistency_runs=4,
    )
    runtime = AgenticRuntime(cfg)
    try:
        with runtime.activate():
            assert len(TextChunker(tokens_per_word=1).chunk("one two three four five six seven eight")) == 2
            assert (
                len(
                    TextChunker(chunk_size=8, chunk_overlap=0, tokens_per_word=1).chunk(
                        "one two three four five six seven eight"
                    )
                )
                == 1
            )
            strategy = MapReduceStrategy(Agent())
            assert len(strategy._chunker.chunk("one two three four five six seven eight")) > 1
            window = SlidingWindowManager()
            window.add("one two three four five six seven eight")
            window.add("latest")
            assert window.get_context() == "latest"
            assert not (await QoSGuard(confidence_scorer=Scorer()).evaluate("answer")).passed
            assert (await QoSGuard(confidence_scorer=Scorer(), min_confidence=0.6).evaluate("answer")).passed
            await ConsistencyChecker(Agent()).check("same")
            assert len(calls) == 4
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_templates_dir", "custom"),
        ("validation_enabled", True),
        ("default_embedding_model", "openai:custom"),
        ("default_vector_store", "memory"),
        ("vector_store_namespace", "custom"),
    ],
)
def test_inert_selector_configuration_is_rejected_with_explicit_api_guidance(field, value):
    with pytest.raises(ValueError, match="removed"):
        AgenticConfig(**{field: value})


@pytest.mark.asyncio
async def test_failed_agent_plugin_restores_preexisting_registry_entry(monkeypatch):
    from pydantic_ai.models.test import TestModel

    from pyfly.agentic import Agent
    from pyfly.agentic.plugin import ENTRY_POINT_AGENTS, DiscoveredPlugin, DiscoveryResult, PluginDiscovery
    from pyfly.context.lifecycle import post_construct

    class FailingAgent(Agent):
        def __init__(self):
            super().__init__("same-name", model=TestModel())

        @post_construct
        async def initialize(self):
            raise ValueError("plugin not initialized")

    monkeypatch.setattr(
        PluginDiscovery,
        "discover_all",
        lambda: DiscoveryResult([DiscoveredPlugin(ENTRY_POINT_AGENTS, "failure", "test:FailingAgent", FailingAgent)]),
    )
    runtime = AgenticRuntime(AgenticConfig())
    previous = runtime.agent_factory.create("same-name", model=TestModel())
    try:
        await runtime.start()
        assert len(runtime.discovery.failed) == 1
        assert runtime.agent_registry.get("same-name") is previous
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sync_factory_agent_awaits_native_memory_on_application_owner_loop():
    from pydantic_ai.models.test import TestModel
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.agentic.memory.relational_store import RelationalMemoryStore

    engine = create_async_engine("sqlite+aiosqlite://")
    store = RelationalMemoryStore(engine, create_table=True)
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), memory_store=store)
    await runtime.start()
    try:
        await runtime.memory.async_set_fact("policy", "local fact")

        class FactMiddleware:
            async def before_run(self, context):
                await runtime.memory.async_set_fact("during-run", context.prompt)

        agent = runtime.agent_factory.create("sync-native-memory", model=TestModel(), middleware=[FactMiddleware()])
        assert (await asyncio.to_thread(agent.run_sync, "hello")).output
        assert await runtime.memory.async_get_fact("during-run") == "hello"
    finally:
        await runtime.aclose()
        await store.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_events", [False, True])
async def test_sync_agent_keeps_native_http_requests_on_application_loop(custom_events):
    from pydantic_ai.models.test import TestModel

    owner = asyncio.get_running_loop()

    class LoopBoundHttp(BorrowedHttp):
        async def request(self, method, url, **kwargs):
            assert asyncio.get_running_loop() is owner
            return await super().request(method, url, **kwargs)

    client = LoopBoundHttp()
    event_loops = []
    bus = ApplicationEventBus()

    async def listener(event):
        event_loops.append(asyncio.get_running_loop())

    bus.subscribe(AgentEvent, listener)
    runtime = AgenticRuntime(
        AgenticConfig(plugin_auto_discover=False),
        http_client=client,
        event_publisher=ApplicationEventPublisher(bus),
        events=AgentEvents(publisher=ApplicationEventPublisher(bus)) if custom_events else None,
    )
    await runtime.start()
    try:
        with runtime.activate():
            tool = HttpTool()
        agent = runtime.agent_factory.create("sync-host-http", model=TestModel(call_tools=["http"]), tools=[tool])
        assert (await asyncio.to_thread(agent.run_sync, "use the local fake port")).output
        assert len(client.requests) == 1
        assert event_loops and all(loop is owner for loop in event_loops)
    finally:
        await runtime.aclose()
    assert client.closed == 0


@pytest.mark.asyncio
async def test_cancelled_native_run_emits_one_error_without_completion():
    from pydantic_ai.models.function import FunctionModel

    received = []
    bus = ApplicationEventBus()

    async def listener(event):
        received.append(event.event_type)

    bus.subscribe(AgentEvent, listener)
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), event_publisher=ApplicationEventPublisher(bus))
    await runtime.start()
    entered = asyncio.Event()

    async def respond(messages, info):
        entered.set()
        await asyncio.Event().wait()

    agent = runtime.agent_factory.create("cancelled", model=FunctionModel(respond))
    task = asyncio.create_task(agent.run("hello"))
    await entered.wait()
    await runtime.aclose()
    assert task.cancelled()
    assert received == ["agent.started", "agent.error"]


@pytest.mark.asyncio
async def test_host_dispatch_loop_is_unbound_before_start_and_rejects_unavailable_owner():
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    assert runtime._application_loop is None
    await runtime.start()
    assert runtime._application_loop is asyncio.get_running_loop()
    unavailable = asyncio.new_event_loop()
    runtime._application_loop = unavailable

    async def operation():
        raise AssertionError("must reject before starting host work")

    try:
        with pytest.raises(RuntimeError, match="owning event loop is unavailable"):
            await runtime.on_application_loop(operation)
    finally:
        runtime._application_loop = asyncio.get_running_loop()
        unavailable.close()
        await runtime.aclose()


def test_database_backend_without_native_data_extra_fails_with_installation_guidance():
    import subprocess
    import sys

    script = """import importlib.abc, sys
class NoSQL(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise ImportError("optional native SQL extra is absent")
sys.meta_path.insert(0, NoSQL())
from pyfly.agentic import AgenticRuntime, AgenticConfig
from pyfly.core.config import Config
try:
    AgenticRuntime(AgenticConfig(memory_backend="relational"), framework_config=Config())
except RuntimeError as exc:
    assert "pyfly[agentic-relational]" in str(exc), str(exc)
else:
    raise AssertionError("missing native dependency accepted")
"""
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_explicit_logging_helper_uses_active_default_and_preserves_explicit_level():
    import io
    import logging

    from pyfly.agentic.logging import configure_logging

    logger = logging.getLogger("pyfly.agentic")
    root = logging.getLogger()
    previous_handlers, previous_level = list(logger.handlers), logger.level
    root_handlers, root_level = list(root.handlers), root.level
    runtime = AgenticRuntime(AgenticConfig(log_level="DEBUG", plugin_auto_discover=False))
    output = io.StringIO()
    try:
        with runtime.activate():
            configure_logging(stream=output)
            logger.debug("native default visible")
            configure_logging("ERROR", stream=output)
            logger.debug("explicit override filtered")
            logger.error("explicit override visible")
        assert "native default visible" in output.getvalue()
        assert "explicit override filtered" not in output.getvalue()
        assert "explicit override visible" in output.getvalue()
        assert root.handlers == root_handlers
        assert root.level == root_level
    finally:
        for handler in list(logger.handlers):
            if handler not in previous_handlers:
                handler.close()
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        await runtime.aclose()
