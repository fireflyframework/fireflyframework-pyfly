"""Regressions from the independent native-runtime review."""

import asyncio

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers import Provider

from pyfly.agentic import Agent, AgenticConfig, AgenticRuntime
from pyfly.agentic.config import get_config
from pyfly.agentic.exceptions import RateLimitError


@pytest.mark.parametrize("streaming", [False, True])
async def test_direct_workflow_runner_selects_owner_with_foreign_runtime_active(streaming):
    owner = AgenticRuntime(AgenticConfig(default_chunk_size=991, quota_enabled=True, quota_rate_limits={"test": 1}))
    foreign = AgenticRuntime(AgenticConfig(default_chunk_size=992))
    seen = []

    def check() -> str:
        seen.append(get_config().default_chunk_size)
        return "ok"

    try:
        with foreign.activate():
            if streaming:
                async with owner.workflow_runner.run_stream(
                    "hello", model=TestModel(call_tools=["check"]), tools=[check]
                ) as stream:
                    async for _ in stream.text():
                        pass
            else:
                await owner.workflow_runner.run("hello", model=TestModel(call_tools=["check"]), tools=[check])
        assert seen == [991]
        assert len(owner.usage_tracker.records) == 1
        assert not foreign.usage_tracker.records
        assert owner.quota_manager.get_rate_limit_remaining("test") == 0
    finally:
        await owner.aclose()
        await foreign.aclose()


@pytest.mark.parametrize("method", ["run", "run_sync", "run_stream", "workflow"])
async def test_per_call_model_controls_quota_and_usage(method):
    runtime = AgenticRuntime(AgenticConfig(quota_enabled=True, quota_rate_limits={"original": 10, "alternate": 1}))
    agent = runtime.agent_factory.create("overridden", model=TestModel(model_name="original"))
    alternate = TestModel(model_name="alternate")

    async def invoke():
        if method == "run_sync":
            return await asyncio.to_thread(agent.run_sync, "hello", model=alternate)
        if method == "run_stream":
            async with await agent.run_stream("hello", model=alternate) as stream:
                await stream.get_output()
        elif method == "workflow":
            return await runtime.workflow_runner.run("hello", using=agent, model=alternate)
        else:
            return await agent.run("hello", model=alternate)

    try:
        await invoke()
        with pytest.raises(RateLimitError):
            await invoke()
        assert runtime.usage_tracker.records[0].model == "alternate"
        assert runtime.quota_manager.get_rate_limit_remaining("original") == 10
    finally:
        await runtime.aclose()


async def test_unmanaged_agent_bean_fails_with_factory_guidance_without_global_registration():
    from pyfly.agentic.agents.registry import agent_registry
    from pyfly.container import bean, configuration
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    @configuration
    class BareConfiguration:
        @bean
        def bare(self) -> Agent:
            return Agent("bare-review", model=TestModel())

    context = ApplicationContext(Config({"pyfly": {"agentic": {"enabled": True, "plugin-auto-discover": False}}}))
    context.register_bean(BareConfiguration)
    try:
        with pytest.raises(Exception, match="AgentFactory"):
            await context.start()
        assert not agent_registry.has("bare-review")
    finally:
        await context.stop()


async def test_cancelled_close_caller_does_not_cancel_shared_resource_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()

    class Resource:
        calls = 0
        finished = False

        async def aclose(self):
            self.calls += 1
            entered.set()
            await release.wait()
            self.finished = True

    runtime = AgenticRuntime(AgenticConfig())
    resource = runtime.own(Resource())
    owner = asyncio.create_task(runtime.aclose())
    await entered.wait()
    waiter = asyncio.create_task(runtime.aclose())
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    await waiter
    await runtime.aclose()
    assert resource.calls == 1
    assert resource.finished
    assert runtime._closed


class FictionalProvider(Provider):
    @property
    def name(self):
        return "fictional"

    @property
    def base_url(self):
        return "http://fictional.invalid"

    @property
    def client(self):
        return None


class LoopBoundTestModel(TestModel):
    """Provider-shaped model whose client permits one event loop."""

    def __init__(self):
        super().__init__()
        self.owner_loop = None
        self._provider = FictionalProvider()

    @property
    def provider(self):
        return self._provider

    def _check_loop(self):
        current = asyncio.get_running_loop()
        if self.owner_loop is None:
            self.owner_loop = current
        assert self.owner_loop is current, "provider client moved between event loops"

    async def request(self, *args, **kwargs):
        self._check_loop()
        return await super().request(*args, **kwargs)

    def request_stream(self, *args, **kwargs):
        self._check_loop()
        return super().request_stream(*args, **kwargs)


async def test_provider_stream_then_sync_bridge_keeps_client_on_original_loop():
    runtime = AgenticRuntime(AgenticConfig())
    model = LoopBoundTestModel()
    agent = runtime.agent_factory.create("affinity", model=model)
    try:
        async with await agent.run_stream("hello") as stream:
            await stream.get_output()
        assert (await asyncio.to_thread(agent.run_sync, "again")).output
        worker = runtime.sync_worker.loop
        assert worker is not None
        assert model.owner_loop is asyncio.get_running_loop()
    finally:
        await runtime.aclose()
    assert worker.is_closed()


async def test_provider_sync_then_async_run_uses_worker_and_cross_loop_stream_fails_clearly():
    runtime = AgenticRuntime(AgenticConfig())
    model = LoopBoundTestModel()
    agent = runtime.agent_factory.create("reverse-affinity", model=model)
    try:
        assert (await asyncio.to_thread(agent.run_sync, "one")).output
        assert (await agent.run("two")).output
        assert model.owner_loop is runtime.sync_worker.loop
        with pytest.raises(Exception, match="stream.*owning event loop"):
            await agent.run_stream("three")
    finally:
        await runtime.aclose()


async def test_sync_cannot_block_provider_owning_loop_and_external_client_remains_open():
    runtime = AgenticRuntime(AgenticConfig())
    model = LoopBoundTestModel()
    agent = runtime.agent_factory.create("same-owner", model=model)
    try:
        await agent.run("one")
        with pytest.raises(Exception, match="owning event loop"):
            agent.run_sync("two")
    finally:
        await runtime.aclose()
    assert model.owner_loop is asyncio.get_running_loop()


async def test_closed_provider_owner_fails_before_reusing_a_client_on_another_loop():
    runtime = AgenticRuntime(AgenticConfig())
    model = LoopBoundTestModel()
    agent = runtime.agent_factory.create("closed-owner", model=model)
    await asyncio.to_thread(lambda: asyncio.run(agent.run("one")))
    try:
        with pytest.raises(Exception, match="owning event loop is closed"):
            await agent.run("two")
        with pytest.raises(Exception, match="owning event loop is closed"):
            await asyncio.to_thread(agent.run_sync, "three")
        assert runtime.sync_worker.loop is None
    finally:
        await runtime.aclose()


async def test_native_selector_owns_its_sdk_client_but_supplied_model_remains_external(monkeypatch):
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "fictional")
    runtime = AgenticRuntime(AgenticConfig())
    native = runtime.agent_factory.create("native-model", model="openai:fictional")
    external_model = OpenAIChatModel("fictional", provider=OpenAIProvider(api_key="fictional"))
    external = runtime.agent_factory.create("external-model", model=external_model)
    native_http = native.agent.model.provider.client._client
    external_http = external.agent.model.provider.client._client
    try:
        await runtime.aclose()
        await runtime.aclose()
        assert native_http.is_closed
        assert not external_http.is_closed
    finally:
        await external_http.aclose()


@pytest.mark.parametrize("explicit_config", [False, True])
async def test_direct_agent_bean_with_explicit_runtime_has_full_native_state(explicit_config):
    from pyfly.agentic.agents.registry import agent_registry
    from pyfly.container import bean, configuration
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    @configuration
    class ManagedConfiguration:
        @bean
        def managed(self, runtime: AgenticRuntime) -> Agent:
            return Agent(
                "explicit-owner",
                runtime=runtime,
                model=TestModel(),
                config=AgenticConfig(default_chunk_size=777) if explicit_config else None,
            )

    context = ApplicationContext(
        Config({"pyfly": {"agentic": {"enabled": True, "default-chunk-size": 778, "plugin-auto-discover": False}}})
    )
    context.register_bean(ManagedConfiguration)
    await context.start()
    try:
        agent = context.get_bean(Agent)
        runtime = context.get_bean(AgenticRuntime)
        assert agent.config.default_chunk_size == (777 if explicit_config else 778)
        assert agent in runtime._agents
        assert runtime.agent_registry.get("explicit-owner") is agent
        assert not agent_registry.has("explicit-owner")
    finally:
        await context.stop()


async def test_failed_resource_cleanup_is_retried_without_reclosing_successful_resources():
    class Resource:
        def __init__(self, fail=False):
            self.calls = 0
            self.fail = fail

        async def aclose(self):
            self.calls += 1
            if self.fail:
                self.fail = False
                raise RuntimeError("close interrupted")

    runtime = AgenticRuntime(AgenticConfig())
    successful = runtime.own(Resource())
    retrying = runtime.own(Resource(fail=True))
    with pytest.raises(ExceptionGroup):
        await runtime.aclose()
    assert not runtime._closed
    await runtime.aclose()
    assert runtime._closed
    assert successful.calls == 1
    assert retrying.calls == 2


async def test_injected_service_uses_bound_workflow_runner_for_calls_and_streams():
    from pyfly.agentic.workflows.runner import ManagedAgentRunner
    from pyfly.container import service
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    seen = []

    def check() -> str:
        seen.append(get_config().default_chunk_size)
        return "ok"

    @service
    class AssistantService:
        def __init__(self, runner: ManagedAgentRunner):
            self.runner = runner

        async def invoke(self):
            await self.runner.run("one", model=TestModel(call_tools=["check"]), tools=[check])
            async with self.runner.run_stream("two", model=TestModel(call_tools=["check"]), tools=[check]) as stream:
                async for _ in stream.text():
                    pass

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "default-chunk-size": 991,
                        "plugin-auto-discover": False,
                        "quota-enabled": True,
                        "quota-rate-limits": {"test": 2},
                    }
                }
            }
        )
    )
    context.register_bean(AssistantService)
    await context.start()
    foreign = AgenticRuntime(AgenticConfig(default_chunk_size=992))
    try:
        runtime = context.get_bean(AgenticRuntime)
        service_bean = context.get_bean(AssistantService)
        assert service_bean.runner is runtime.workflow_runner
        with foreign.activate():
            await service_bean.invoke()
        assert seen == [991, 991]
        assert len(runtime.usage_tracker.records) == 2
        assert not foreign.usage_tracker.records
        assert runtime.quota_manager.get_rate_limit_remaining("test") == 0
    finally:
        await context.stop()
        await foreign.aclose()


async def test_explicit_model_ownership_transfer_closes_only_that_models_own_client():
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    runtime = AgenticRuntime(AgenticConfig())
    model = OpenAIChatModel("fictional", provider=OpenAIProvider(api_key="fictional"))
    http_client = model.provider.client._client
    try:
        assert runtime.own(model) is model
        await runtime.aclose()
        assert http_client.is_closed
    finally:
        await http_client.aclose()
