# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Application-owned Agentic state, execution context, and resource lifecycle."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import threading
import weakref
from collections.abc import Awaitable, Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from typing import Any, TypeVar

from pydantic_ai.models import Model

from pyfly.agentic._model_lifecycle import ModelState
from pyfly.agentic._runtime_context import _current, _operation_config
from pyfly.agentic._worker import WorkerLoop
from pyfly.agentic.agents.registry import AgentRegistry
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.explainability.trace_recorder import TraceRecorder
from pyfly.agentic.factory import AgentFactory
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.agentic.memory.store import MemoryStore
from pyfly.agentic.observability.events import AgentEvents
from pyfly.agentic.observability.metrics import AgentMetrics
from pyfly.agentic.observability.quota import QuotaManager
from pyfly.agentic.observability.sinks import OTelMetricsSink
from pyfly.agentic.observability.tracer import AgentTracer
from pyfly.agentic.observability.usage import UsageTracker
from pyfly.agentic.prompts.registry import PromptRegistry
from pyfly.agentic.reasoning.registry import ReasoningPatternRegistry
from pyfly.agentic.tools.registry import ToolRegistry
from pyfly.agentic.workflows.registry import WorkflowRegistry
from pyfly.agentic.workflows.runner import ManagedAgentRunner
from pyfly.client.ports.outbound import HttpClientPort
from pyfly.container.container import Container
from pyfly.context.events import ApplicationEventPublisher
from pyfly.core.config import Config

T = TypeVar("T")


class AgenticRuntime:
    def __init__(
        self,
        config: AgenticConfig,
        *,
        agent_registry: AgentRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        reasoning_registry: ReasoningPatternRegistry | None = None,
        prompt_registry: PromptRegistry | None = None,
        workflow_registry: WorkflowRegistry | None = None,
        usage_tracker: UsageTracker | None = None,
        quota_manager: QuotaManager | None = None,
        memory: MemoryManager | None = None,
        memory_store: MemoryStore | None = None,
        encryption_provider: Any = None,
        http_client: HttpClientPort | None = None,
        event_publisher: ApplicationEventPublisher | None = None,
        events: AgentEvents | None = None,
        container: Container | None = None,
        framework_config: Config | None = None,
        tracer_provider: Any = None,
        meter_provider: Any = None,
    ) -> None:
        self.http_client = http_client
        self.container = container
        self._plugin_resources: list[Any] = []
        self._plugin_entries: list[tuple[Any, str, Any]] = []
        self._application_loop: asyncio.AbstractEventLoop | None = None
        self.config = config
        self._config_hooks: list[Callable[[AgenticConfig], None]] = []
        self._agents: weakref.WeakSet[Any] = weakref.WeakSet()
        self._model_states: dict[int, ModelState] = {}
        self._provider_selector: Any = None
        self.agent_registry = agent_registry if agent_registry is not None else AgentRegistry()
        self.tool_registry = tool_registry if tool_registry is not None else ToolRegistry()
        self.reasoning_registry = reasoning_registry if reasoning_registry is not None else ReasoningPatternRegistry()
        self.prompt_registry = prompt_registry if prompt_registry is not None else PromptRegistry()
        self.workflow_registry = workflow_registry if workflow_registry is not None else WorkflowRegistry()
        self.usage_tracker = (
            usage_tracker if usage_tracker is not None else UsageTracker.for_config(config, sinks=[OTelMetricsSink()])
        )
        self.quota_manager = (
            quota_manager
            if quota_manager is not None
            else (
                QuotaManager(
                    rate_limits=config.quota_rate_limits, enable_adaptive_backoff=config.quota_adaptive_backoff
                )
                if config.quota_enabled
                else None
            )
        )
        self.tracer_provider, self.meter_provider = tracer_provider, meter_provider
        self.tracer = AgentTracer(tracer_provider=tracer_provider)
        self.metrics = AgentMetrics(meter_provider=meter_provider)
        self.events = events if events is not None else AgentEvents(publisher=event_publisher)
        if events is None:
            self.events._dispatcher = self.on_application_loop
        self.trace_recorder = TraceRecorder()
        from pyfly.agentic.security.encryption import create_encryption_provider_from_config

        token = _current.set(self)
        try:
            self.encryption_provider = (
                encryption_provider
                if encryption_provider is not None
                else (create_encryption_provider_from_config() if memory is None else None)
            )
        finally:
            _current.reset(token)
        self.sync_worker = WorkerLoop("pyfly-agentic-sync")
        self.database_worker = WorkerLoop("pyfly-agentic-database")
        self._resources: dict[int, Any] = {}
        self._closed_resources: set[int] = set()
        self._state_lock = threading.RLock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._streams: set[Any] = set()
        self._entry_tokens: ContextVar[tuple[Token[AgenticRuntime | None], ...]] = ContextVar(
            "pyfly_agentic_entries", default=()
        )
        self._closing = False
        self._close_waiter: Future[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._started = False
        self.discovery: Any = None
        borrowed_memory_store = memory_store is not None
        with self.activate():
            if (
                memory is None
                and memory_store is None
                and config.memory_backend in ("relational", "postgres", "mongodb")
            ):
                from pyfly.agentic._framework_bindings import native_memory_store

                memory_store = native_memory_store(config, framework_config, container)
            self.memory = (
                memory
                if memory is not None
                else MemoryManager.from_config(
                    config,
                    initialize=False,
                    store=memory_store,
                    encryption_provider=self.encryption_provider,
                    owns_store=not borrowed_memory_store,
                )
            )
        self._owns_memory = memory is None
        if self._owns_memory and (not borrowed_memory_store or self.memory.store is not memory_store):
            self.own(self.memory.store)
        self.agent_factory = AgentFactory(self)
        self.workflow_runner = ManagedAgentRunner(
            registry=self.agent_registry, default_model=config.default_model, runtime=self
        )

    def bind_providers(self, tracer_provider: Any, meter_provider: Any) -> None:
        self.tracer_provider, self.meter_provider = tracer_provider, meter_provider
        self.tracer = AgentTracer(tracer_provider=tracer_provider)
        self.metrics = AgentMetrics(meter_provider=meter_provider)
        for agent in self._agents:
            for settings in agent._native_instrumentation_settings:
                settings.tracer_provider = tracer_provider
                settings.meter_provider = meter_provider

    def configure(self, config: AgenticConfig) -> None:
        self.config = config
        self.usage_tracker.apply_config(config)
        for hook in list(self._config_hooks):
            hook(config)

    @contextmanager
    def activate(self) -> Generator[AgenticRuntime]:
        if self._closed:
            raise RuntimeError("Agentic runtime is closed")
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    @contextmanager
    def operation(self, config: AgenticConfig | None = None) -> Generator[None]:
        if self._closing:
            raise RuntimeError("Agentic runtime is shutting down")
        with self.activate():
            previous_config = _operation_config.get()
            effective = (
                config
                if config is not None
                else (previous_config[1] if previous_config is not None and previous_config[0] is self else self.config)
            )
            config_token = _operation_config.set((self, effective))
            task = asyncio.current_task()
            with self._state_lock:
                already = task in self._tasks if task else False
                if task is not None:
                    self._tasks.add(task)
            try:
                yield
            finally:
                _operation_config.reset(config_token)
                if task is not None and not already:
                    with self._state_lock:
                        self._tasks.discard(task)

    def own(self, resource: T) -> T:
        if isinstance(resource, Model):
            state = self._model_states.setdefault(id(resource), ModelState(resource, owned=True))
            state.owned = True
            self._resources[id(resource)] = state
        else:
            self._resources[id(resource)] = resource
        return resource

    def external(self, resource: T) -> T:
        self._resources.pop(id(resource), None)
        if isinstance(resource, Model):
            state = self._model_states.get(id(resource))
            if state is not None:
                self._resources.pop(id(state), None)
                state.owned = False
        return resource

    async def on_application_loop(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Await a host-service operation on the application's owning loop."""
        loop = self._application_loop

        async def execute() -> T:
            with self.activate():
                task = asyncio.current_task()
                with self._state_lock:
                    already = task in self._tasks
                    if task is not None:
                        self._tasks.add(task)
                try:
                    return await operation()
                finally:
                    if task is not None and not already:
                        with self._state_lock:
                            self._tasks.discard(task)

        if loop is None or loop is asyncio.get_running_loop():
            return await execute()
        if not loop.is_running():
            raise RuntimeError("The host application's owning event loop is unavailable")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(execute(), loop))

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("Agentic runtime is shutting down")
        if self._started:
            return
        self._closed = False
        self._application_loop = asyncio.get_running_loop()
        try:
            with self.activate():
                restarted = {id(state) for state in self._model_states.values() if state.restart()}
                for identity, resource in self._resources.items():
                    if isinstance(resource, ModelState) and id(resource) in restarted:
                        self._closed_resources.discard(identity)
                if self.container is not None and self.http_client is None:
                    from pyfly.container.exceptions import NoSuchBeanError

                    with suppress(NoSuchBeanError):
                        self.http_client = self.container.resolve(HttpClientPort)  # type: ignore[type-abstract]
                if self._provider_selector is not None:
                    self.bind_providers(*self._provider_selector())
                if self._owns_memory:
                    initialize = getattr(self.memory.store, "initialize", None)
                    if initialize is not None:
                        self._closed_resources.discard(id(self.memory.store))
                        await initialize()
                for registry, name, instance in self._plugin_entries:
                    if registry.has(name) and registry.get(name) is instance:
                        registry.unregister(name)
                self._plugin_entries.clear()
                for resource in self._plugin_resources:
                    if not resource.closed:
                        await resource.aclose()
                    self._resources.pop(id(resource), None)
                    self._closed_resources.discard(id(resource))
                self._plugin_resources.clear()
                if self.config.plugin_auto_discover:
                    from pyfly.agentic.plugin import PluginDiscovery

                    self.discovery = PluginDiscovery.discover_all()
                    await self._register_plugins()
            self._started = True
        except BaseException:
            await self.aclose()
            raise

    async def _register_plugins(self) -> None:
        from pyfly.agentic.agents.base import Agent
        from pyfly.agentic.plugin import ENTRY_POINT_AGENTS, ENTRY_POINT_REASONING, ENTRY_POINT_TOOLS

        for plugin in self.discovery.successful:
            resource = None
            try:
                loaded = plugin.loaded_object
                if inspect.isclass(loaded) or inspect.isfunction(loaded) or inspect.ismethod(loaded):
                    from pyfly.agentic._framework_bindings import PluginResource, construct_plugin

                    loaded, borrowed = await construct_plugin(loaded, self.container)
                    if not borrowed:
                        resource = self.own(PluginResource(loaded))
                        self._plugin_resources.append(resource)
                        await resource.initialize()
                elif isinstance(loaded, Agent):
                    if loaded._runtime is not self:
                        raise ValueError(
                            "Managed agent plugins must export a factory that creates an application-owned Agent"
                        )
                else:
                    # Imported module attributes are cached across application contexts.
                    # Copy static tools/patterns rather than sharing their mutable state.
                    loaded = copy.deepcopy(loaded)
                if plugin.group == ENTRY_POINT_AGENTS:
                    if not isinstance(loaded, Agent) or loaded._runtime is not self:
                        raise TypeError("Agent plugin must create an Agent bound to the selected runtime")
                    self.agent_registry.register(loaded)
                    self._plugin_entries.append((self.agent_registry, loaded.name, loaded))
                elif plugin.group == ENTRY_POINT_TOOLS:
                    self.tool_registry.register(loaded)
                    self._plugin_entries.append((self.tool_registry, loaded.name, loaded))
                elif plugin.group == ENTRY_POINT_REASONING:
                    self.reasoning_registry.register(plugin.name, loaded)
                    self._plugin_entries.append((self.reasoning_registry, plugin.name, loaded))
                plugin.loaded_object = loaded
            except Exception as exc:
                if (
                    isinstance(loaded, Agent)
                    and loaded._runtime is self
                    and self.agent_registry.has(loaded.name)
                    and self.agent_registry.get(loaded.name) is loaded
                ):
                    self.agent_registry.unregister(loaded.name)
                    previous_ref = getattr(loaded, "_registration_predecessor", None)
                    previous = previous_ref() if previous_ref is not None else None
                    if previous is not None:
                        self.agent_registry.register(previous)
                if resource is not None:
                    try:
                        await resource.aclose()
                    except Exception:
                        logging.getLogger(__name__).warning(
                            "Failed to clean up Agentic plugin %s", plugin.name, exc_info=True
                        )
                plugin.error = f"{type(exc).__name__}: {exc}"
                plugin.loaded_object = None
                logging.getLogger(__name__).warning("Failed to initialize Agentic plugin %s: %s", plugin.name, exc)

    async def stop(self) -> None:
        await self.aclose()

    async def __aenter__(self) -> AgenticRuntime:
        await self.start()
        self._entry_tokens.set((*self._entry_tokens.get(), _current.set(self)))
        return self

    async def __aexit__(self, *args: Any) -> None:
        tokens = self._entry_tokens.get()
        try:
            await self.aclose()
        finally:
            if tokens:
                _current.reset(tokens[-1])
                self._entry_tokens.set(tokens[:-1])

    async def aclose(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            if self._close_waiter is None:
                waiter: Future[None] = Future()
                self._close_waiter = waiter
                self._closing = True
                self._close_task = asyncio.create_task(self._close_owned(asyncio.current_task()))
                self._close_task.add_done_callback(lambda task: self._finish_close(task, waiter))
            else:
                waiter = self._close_waiter
        await asyncio.shield(asyncio.wrap_future(waiter))

    def _finish_close(self, task: asyncio.Task[None], waiter: Future[None]) -> None:
        try:
            task.result()
        except BaseException as exc:
            waiter.set_exception(exc)
        else:
            waiter.set_result(None)
        finally:
            with self._state_lock:
                self._close_waiter = None
                self._close_task = None
                self._closing = False

    async def _close_owned(self, initiator: asyncio.Task[Any] | None = None) -> None:
        if self._closed:
            return
        current = asyncio.current_task()
        with self._state_lock:
            tasks = [t for t in self._tasks if t is not current and t is not initiator and not t.done()]
        for task in tasks:
            task.get_loop().call_soon_threadsafe(task.cancel)
        local_tasks = [t for t in tasks if t.get_loop() is asyncio.get_running_loop()]
        if local_tasks:
            await asyncio.wait(local_tasks, timeout=5)

        async def drain(task: asyncio.Task[Any]) -> None:
            await asyncio.gather(task, return_exceptions=True)

        foreign = [t for t in tasks if t.get_loop() is not asyncio.get_running_loop() and not t.done()]
        for task in foreign:
            future = asyncio.run_coroutine_threadsafe(drain(task), task.get_loop())
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
            except TimeoutError:
                future.cancel()
        errors: list[Exception] = []
        with self.activate():
            for stream in list(self._streams):
                try:
                    await stream.aclose()
                except Exception as exc:
                    errors.append(exc)
            for identity, resource in reversed(list(self._resources.items())):
                if identity in self._closed_resources:
                    continue
                close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
                if close is not None:
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await asyncio.wait_for(result, timeout=5)
                        self._closed_resources.add(identity)
                    except Exception as exc:
                        errors.append(exc)
        await asyncio.to_thread(self.sync_worker.close)
        await asyncio.to_thread(self.database_worker.close)
        self._closed = not errors
        self._started = False
        if errors:
            raise ExceptionGroup("Agentic runtime shutdown failed", errors)
