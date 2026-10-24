# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Enabled-only bean definitions; imported after the optional dependency guard."""

from __future__ import annotations

from typing import Any

from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import TracerProvider

from pyfly.agentic.agents.registry import AgentRegistry
from pyfly.agentic.auto_configuration import bind_agentic_config
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.factory import AgentFactory
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.agentic.memory.store import MemoryStore
from pyfly.agentic.observability.events import AgentEvents
from pyfly.agentic.observability.quota import QuotaManager
from pyfly.agentic.observability.sinks import OTelMetricsSink
from pyfly.agentic.observability.usage import UsageTracker
from pyfly.agentic.prompts.registry import PromptRegistry
from pyfly.agentic.reasoning.registry import ReasoningPatternRegistry
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.agentic.security.encryption import EncryptionProvider
from pyfly.agentic.tools.builtins.http import HttpTool
from pyfly.agentic.tools.registry import ToolRegistry
from pyfly.agentic.workflows.registry import WorkflowRegistry
from pyfly.agentic.workflows.runner import ManagedAgentRunner
from pyfly.client.ports.outbound import HttpClientPort
from pyfly.container import bean
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.context.conditions import conditional_on_missing_bean, conditional_on_property
from pyfly.context.events import ApplicationEventPublisher
from pyfly.core.config import Config


class AgenticBeanPostProcessor:
    singletons_only = True

    def __init__(self, runtime: AgenticRuntime) -> None:
        self.runtime = runtime

    def before_init(self, bean: Any, bean_name: str) -> Any:
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        from pyfly.agentic.agents.base import Agent

        if isinstance(bean, Agent):
            if bean._runtime is not self.runtime:
                self._remove_registration(bean)
                raise ValueError(
                    "Agent bean must be created by this application's AgentFactory or pass runtime= explicitly"
                )
            self.runtime._agents.add(bean)
            self.runtime.agent_registry.register(bean)
        return bean

    @staticmethod
    def _remove_registration(bean: Any) -> None:
        from pyfly.agentic.agents.registry import agent_registry

        registry = bean._runtime.agent_registry if bean._runtime is not None else agent_registry
        if registry.has(bean.name) and registry.get(bean.name) is bean:
            registry.unregister(bean.name)
            previous_ref = getattr(bean, "_registration_predecessor", None)
            previous = previous_ref() if previous_ref is not None else None
            if previous is not None:
                registry.register(previous)

    def non_singleton_skipped(self, bean: Any, bean_name: str, scope: Any) -> None:
        from pyfly.agentic.agents.base import Agent

        if not isinstance(bean, Agent):
            return
        self._remove_registration(bean)
        if bean._runtime is not self.runtime:
            raise ValueError(
                "Agent bean must be created by this application's AgentFactory or pass runtime= explicitly"
            )
        self.runtime._agents.add(bean)


class NativeAgenticAutoConfiguration:
    @bean
    @conditional_on_missing_bean(AgenticConfig)
    def agentic_config(self, config: Config) -> AgenticConfig:
        return bind_agentic_config(config)

    @bean
    @conditional_on_missing_bean(AgentRegistry)
    def agentic_agent_registry(self) -> AgentRegistry:
        return AgentRegistry()

    @bean
    @conditional_on_missing_bean(ToolRegistry)
    def agentic_tool_registry(self) -> ToolRegistry:
        return ToolRegistry()

    @bean
    @conditional_on_missing_bean(ReasoningPatternRegistry)
    def agentic_reasoning_registry(self) -> ReasoningPatternRegistry:
        return ReasoningPatternRegistry()

    @bean
    @conditional_on_missing_bean(PromptRegistry)
    def agentic_prompt_registry(self) -> PromptRegistry:
        return PromptRegistry()

    @bean
    @conditional_on_missing_bean(WorkflowRegistry)
    def agentic_workflow_registry(self) -> WorkflowRegistry:
        return WorkflowRegistry()

    @bean
    @conditional_on_missing_bean(UsageTracker)
    def agentic_usage_tracker(self, config: AgenticConfig) -> UsageTracker:
        return UsageTracker.for_config(config, sinks=[OTelMetricsSink()])

    @bean
    @conditional_on_missing_bean(QuotaManager)
    @conditional_on_property("pyfly.agentic.quota-enabled", having_value="true")
    def agentic_quota(self, config: AgenticConfig) -> QuotaManager:
        return QuotaManager(rate_limits=config.quota_rate_limits, enable_adaptive_backoff=config.quota_adaptive_backoff)

    @bean(destroy_method="aclose")
    @conditional_on_missing_bean(AgenticRuntime)
    def agentic_runtime(
        self,
        config: AgenticConfig,
        container: Container,
        agents: AgentRegistry,
        tools: ToolRegistry,
        reasoning: ReasoningPatternRegistry,
        prompts: PromptRegistry,
        workflows: WorkflowRegistry,
        usage: UsageTracker,
        memory: MemoryManager | None = None,
        memory_store: MemoryStore | None = None,
        encryption_provider: EncryptionProvider | None = None,
        http_client: HttpClientPort | None = None,
        event_publisher: ApplicationEventPublisher | None = None,
        events: AgentEvents | None = None,
        quota: QuotaManager | None = None,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> AgenticRuntime:
        runtime = AgenticRuntime(
            config,
            agent_registry=agents,
            tool_registry=tools,
            reasoning_registry=reasoning,
            prompt_registry=prompts,
            workflow_registry=workflows,
            usage_tracker=usage,
            memory=memory,
            memory_store=memory_store,
            encryption_provider=encryption_provider,
            http_client=http_client,
            event_publisher=event_publisher,
            events=events,
            container=container,
            framework_config=container.resolve(Config),
            quota_manager=quota,
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )

        def providers() -> tuple[Any, Any]:
            selected: list[Any] = []
            for provider_type in (TracerProvider, MeterProvider):
                try:
                    selected.append(container.resolve(provider_type))
                except NoSuchBeanError:
                    selected.append(None)
            return selected[0], selected[1]

        runtime._provider_selector = providers
        return runtime

    @bean
    @conditional_on_missing_bean(AgentFactory)
    def agentic_factory(self, runtime: AgenticRuntime) -> AgentFactory:
        return runtime.agent_factory

    @bean
    @conditional_on_missing_bean(MemoryManager)
    def agentic_memory(self, runtime: AgenticRuntime) -> MemoryManager:
        return runtime.memory

    @bean
    @conditional_on_missing_bean(ManagedAgentRunner)
    def agentic_workflow_runner(self, runtime: AgenticRuntime) -> ManagedAgentRunner:
        return runtime.workflow_runner

    @bean
    def agentic_post_processor(self, runtime: AgenticRuntime) -> AgenticBeanPostProcessor:
        return AgenticBeanPostProcessor(runtime)

    @bean
    @conditional_on_missing_bean(HttpTool)
    def agentic_http_tool(self, runtime: AgenticRuntime) -> HttpTool:
        with runtime.activate():
            return HttpTool(use_pool=False)
