# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ApplicationContext — the central bean registry and lifecycle manager."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import typing
from collections import deque
from typing import Any, TypeVar

from pyfly.container.container import Container
from pyfly.container.exceptions import (
    BeanCreationException,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.ordering import get_order
from pyfly.container.types import Scope
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.environment import Environment
from pyfly.context.events import (
    ApplicationEvent,
    ApplicationEventBus,
    ApplicationReadyEvent,
    ContextClosedEvent,
    ContextRefreshedEvent,
)
from pyfly.context.post_processor import BeanPostProcessor
from pyfly.core.config import Config

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ApplicationContext:
    """Central bean registry, lifecycle manager, and event publisher.

    This is the PyFly equivalent of Spring's ApplicationContext. It wraps
    the DI Container and adds:
    - Named bean access
    - @Bean factory method resolution from @configuration classes
    - @post_construct / @pre_destroy lifecycle
    - BeanPostProcessor hooks
    - Application event publishing
    - Profile-aware Environment
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._container = Container()
        self._environment = Environment(config)
        self._event_bus = ApplicationEventBus()
        self._post_processors: list[BeanPostProcessor] = []
        self._started = False
        self._infrastructure_adapters: list[Any] = []
        self._task_scheduler: Any | None = None
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._wiring_counts: dict[str, int] = {}

        # Register config and container as singleton beans (injectable like Spring's ApplicationContext)
        self._container.register(Config, scope=Scope.SINGLETON)
        self._container._registrations[Config].instance = config
        self._container.register(Container, scope=Scope.SINGLETON)
        self._container._registrations[Container].instance = self._container

    # ------------------------------------------------------------------
    # Bean registration
    # ------------------------------------------------------------------

    def register_bean(self, cls: type, **kwargs: Any) -> None:
        """Register a bean class with the context."""
        name: str = kwargs.get("name", "") or getattr(cls, "__pyfly_bean_name__", "")
        scope: Scope = kwargs.get("scope") or getattr(cls, "__pyfly_scope__", Scope.SINGLETON)  # type: ignore[assignment]
        self._container.register(cls, scope=scope, name=name)

    def register_post_processor(self, processor: BeanPostProcessor) -> None:
        """Register a BeanPostProcessor."""
        self._post_processors.append(processor)

    # ------------------------------------------------------------------
    # Bean access
    # ------------------------------------------------------------------

    def get_bean(self, bean_type: type[T]) -> T:
        """Resolve a bean by type."""
        return self._container.resolve(bean_type)

    def get_bean_by_name(self, name: str) -> Any:
        """Resolve a bean by its registered name."""
        return self._container.resolve_by_name(name)

    def get_beans_of_type(self, bean_type: type[T]) -> list[T]:
        """Resolve all beans of the given type, sorted by @order."""
        results = self._container.resolve_all(bean_type)
        return sorted(results, key=lambda b: get_order(type(b)))

    def contains_bean(self, name: str) -> bool:
        """Check if a named bean exists."""
        return self._container.contains(name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def container(self) -> Container:
        """Escape hatch: direct access to the underlying Container."""
        return self._container

    @property
    def config(self) -> Config:
        """Application configuration."""
        return self._config

    @property
    def environment(self) -> Environment:
        """Application environment with profile support."""
        return self._environment

    @property
    def event_bus(self) -> ApplicationEventBus:
        """Application event bus."""
        return self._event_bus

    @property
    def bean_count(self) -> int:
        """Number of beans eagerly initialized during start()."""
        return sum(1 for reg in self._container._registrations.values() if reg.instance is not None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the context: resolve @configuration beans, call lifecycle hooks, publish events."""
        try:
            await self._do_start()
        except BeanCreationException:
            raise
        except Exception as exc:
            raise BeanCreationException(
                subsystem="startup",
                provider=type(exc).__qualname__,
                reason=str(exc),
            ) from exc

    async def _do_start(self) -> None:
        """Internal startup logic."""
        # 0. Register built-in @auto_configuration classes
        self._register_auto_configurations()

        # 1. Filter beans by active profiles
        self._filter_by_profile()

        # 1b. Evaluate @conditional_on_* decorators (pass 1: property/class)
        self._evaluate_conditions()

        # 2. Process user @configuration classes and their @bean methods
        self._process_configurations(auto=False)

        # 2b. Process @auto_configuration classes (after user configs, so
        #     @conditional_on_missing_bean can see user-provided beans)
        self._evaluate_bean_conditions()
        self._process_configurations(auto=True)

        # 2c. Start infrastructure adapters (fail-fast: validates connectivity)
        await self._start_infrastructure()

        # 3. Auto-discover BeanPostProcessors from registered beans
        self._discover_post_processors()

        # 4. Eagerly resolve all singletons (sorted by @order)
        sorted_entries = sorted(
            self._container._registrations.items(),
            key=lambda item: get_order(item[0]),
        )
        for cls, reg in sorted_entries:
            if reg.scope == Scope.SINGLETON and reg.instance is None:
                try:
                    self._container.resolve(cls)
                except BeanCreationException as exc:
                    logger.debug("deferred_bean_resolution", extra={"bean": cls.__name__, "reason": str(exc)})

        # 5. Run post-processors and lifecycle hooks.
        #
        # Two passes (not one per-bean loop): every BeanPostProcessor.before_init
        # runs across ALL beans before any after_init. Otherwise weaving is
        # registration-order dependent — a target bean initialized before its
        # @aspect bean would have its advice silently skipped, because the aspect
        # is only collected during the aspect's own before_init. Snapshotting the
        # registrations also avoids "dict changed size during iteration" if a hook
        # lazily creates another bean.
        sorted_pps = sorted(self._post_processors, key=lambda pp: get_order(type(pp)))
        live_regs = [reg for reg in self._container._registrations.values() if reg.instance is not None]

        # Pass 1: before_init for every bean (collects all @aspect beans, etc.)
        for reg in live_regs:
            bean_name = reg.name or reg.impl_type.__name__
            for pp in sorted_pps:
                reg.instance = pp.before_init(reg.instance, bean_name)

        # Pass 2: @post_construct then after_init (weaving now sees every aspect)
        for reg in live_regs:
            bean_name = reg.name or reg.impl_type.__name__
            await self._call_post_construct(reg.instance)
            for pp in sorted_pps:
                reg.instance = pp.after_init(reg.instance, bean_name)

        # 6. Wire decorator-based beans to their targets
        self._wire_app_event_listeners()
        self._wire_message_listeners()
        self._wire_cqrs_handlers()
        self._wire_scheduled()
        self._wire_async_methods()
        self._wire_shell_commands()

        # 7. Publish lifecycle events
        await self._event_bus.publish(ContextRefreshedEvent())
        await self._event_bus.publish(ApplicationReadyEvent())
        await self._invoke_runners()
        self._started = True

    async def stop(self) -> None:
        """Stop the context: call @pre_destroy, publish ContextClosedEvent.

        Each cleanup step is wrapped in a per-bean timeout (default 30s,
        configurable via ``pyfly.context.shutdown-timeout``).  Beans that
        exceed the timeout are logged and skipped so one hanging bean
        cannot block the entire shutdown sequence.
        """
        shutdown_timeout = float(self._config.get("pyfly.context.shutdown-timeout", 30))

        # Cancel tracked background tasks
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Stop task scheduler
        if self._task_scheduler is not None:
            try:
                await asyncio.wait_for(self._task_scheduler.stop(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.warning("task_scheduler_stop_timeout", extra={"timeout_s": shutdown_timeout})
            except Exception:
                logger.debug("task_scheduler_stop_failed", exc_info=True)

        # Stop infrastructure adapters (reverse order)
        for adapter in reversed(self._infrastructure_adapters):
            if hasattr(adapter, "stop"):
                adapter_name = type(adapter).__qualname__
                try:
                    await asyncio.wait_for(adapter.stop(), timeout=shutdown_timeout)
                except TimeoutError:
                    logger.warning(
                        "adapter_stop_timeout",
                        extra={"adapter": adapter_name, "timeout_s": shutdown_timeout},
                    )
                except Exception:
                    logger.debug("adapter_stop_failed", extra={"adapter": adapter_name}, exc_info=True)

        # Call @pre_destroy on all resolved beans (reverse order)
        for reg in reversed(list(self._container._registrations.values())):
            if reg.instance is not None:
                try:
                    await asyncio.wait_for(
                        self._call_pre_destroy(reg.instance),
                        timeout=shutdown_timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        "pre_destroy_timeout",
                        extra={"bean": type(reg.instance).__qualname__, "timeout_s": shutdown_timeout},
                    )

        await self._event_bus.publish(ContextClosedEvent())
        self._started = False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register_auto_configurations(self) -> None:
        """Register built-in @auto_configuration classes for condition evaluation."""
        from pyfly.config.auto import discover_auto_configurations

        for cls in discover_auto_configurations():
            if cls not in self._container._registrations:
                self.register_bean(cls)

    async def _start_infrastructure(self) -> None:
        """Start adapter beans that implement start()/stop() lifecycle."""
        for reg in self._container._registrations.values():
            if reg.instance is None:
                continue
            if self._has_lifecycle_methods(reg.instance):
                self._infrastructure_adapters.append(reg.instance)

        for adapter in self._infrastructure_adapters:
            try:
                await adapter.start()
            except Exception as exc:
                raise BeanCreationException(
                    subsystem=self._infer_subsystem(adapter),
                    provider=type(adapter).__name__,
                    reason=str(exc),
                ) from exc

    @staticmethod
    def _has_lifecycle_methods(instance: object) -> bool:
        """Check if start/stop are defined on the class (not via __getattr__ magic)."""
        cls = type(instance)
        return all(any(attr in vars(c) for c in cls.__mro__) for attr in ("start", "stop"))

    @staticmethod
    def _infer_subsystem(adapter: object) -> str:
        """Infer subsystem from adapter's module path (no hardcoded names).

        Parses the module hierarchy: ``pyfly.<subsystem>.…`` → ``<subsystem>``.
        Third-party or unrecognised modules fall back to ``"infrastructure"``.
        """
        module = type(adapter).__module__ or ""
        parts = module.split(".")
        if len(parts) >= 2 and parts[0] == "pyfly":
            return parts[1]
        return "infrastructure"

    def _filter_by_profile(self) -> None:
        """Remove beans whose profile expression does not match active profiles."""
        to_remove: list[type] = []
        for cls in list(self._container._registrations):
            profile_expr = getattr(cls, "__pyfly_profile__", "")
            if profile_expr and not self._environment.accepts_profiles(profile_expr):
                to_remove.append(cls)

        for cls in to_remove:
            self._remove_registration(cls)

    def _evaluate_conditions(self) -> None:
        """Pass 1: remove beans that fail non-bean-dependent conditions (on_property, on_class)."""
        evaluator = ConditionEvaluator(self._config, self._container)
        to_remove: list[type] = []
        for cls in list(self._container._registrations):
            if not evaluator.should_include(cls, bean_pass=False):
                to_remove.append(cls)
        for cls in to_remove:
            self._remove_registration(cls)

    def _evaluate_bean_conditions(self) -> None:
        """Pass 2: remove beans that fail bean-dependent conditions (on_bean, on_missing_bean)."""
        evaluator = ConditionEvaluator(self._config, self._container)
        to_remove: list[type] = []
        for cls in list(self._container._registrations):
            if not evaluator.should_include(cls, bean_pass=True):
                to_remove.append(cls)
        for cls in to_remove:
            self._remove_registration(cls)

    def _remove_registration(self, cls: type) -> None:
        """Remove a bean registration and its named entry."""
        reg = self._container._registrations.pop(cls)
        if reg.name and reg.name in self._container._named:
            del self._container._named[reg.name]

    def _process_configurations(self, *, auto: bool = False) -> None:
        """Find @configuration beans, call their @bean methods, register results.

        Args:
            auto: When False, process only user @configuration classes.
                  When True, process only @auto_configuration classes.
        """
        evaluator = ConditionEvaluator(self._config, self._container)

        for cls, _reg in list(self._container._registrations.items()):
            if getattr(cls, "__pyfly_stereotype__", "") != "configuration":
                continue
            is_auto = getattr(cls, "__pyfly_auto_configuration__", False)
            if is_auto != auto:
                continue

            # Resolve the configuration class itself
            config_instance: Any = self._container.resolve(cls)

            # Collect @bean methods and sort by dependency order so that
            # beans whose parameters depend on other beans from the same
            # configuration class are created after their dependencies.
            bean_methods: list[tuple[str, Any]] = []
            for attr_name in dir(config_instance):
                method = getattr(config_instance, attr_name, None)
                if method is None or not getattr(method, "__pyfly_bean__", False):
                    continue
                if not evaluator.should_include_method(method):
                    continue
                bean_methods.append((attr_name, method))

            bean_methods = self._sort_bean_methods(bean_methods)

            for attr_name, method in bean_methods:
                # Get return type from method hints
                hints = typing.get_type_hints(method)
                return_type = hints.get("return")
                if return_type is None:
                    continue

                # Call the factory method (inject params from container)
                result = self._call_bean_method(config_instance, method)
                bean_name = getattr(method, "__pyfly_bean_name__", "") or attr_name
                bean_scope = getattr(method, "__pyfly_bean_scope__", Scope.SINGLETON)

                # Register bean: use the concrete type so multiple beans
                # returning the same interface type don't overwrite each other
                impl_type = type(result)
                self._container.register(impl_type, scope=bean_scope, name=bean_name)
                if bean_scope == Scope.SINGLETON:
                    self._container._registrations[impl_type].instance = result

                # Bind return type → concrete type for list[T] resolution
                if return_type is not impl_type:
                    self._container.bind(return_type, impl_type)

                # Also keep a direct registration for the return type
                # (for single-bean resolution) unless it already exists
                if return_type not in self._container._registrations:
                    self._container.register(return_type, scope=bean_scope)
                    if bean_scope == Scope.SINGLETON:
                        self._container._registrations[return_type].instance = result

    def _call_bean_method(self, config_instance: Any, method: Any) -> Any:
        """Call a @bean method, injecting its parameters from the container."""
        hints = typing.get_type_hints(method)
        hints.pop("return", None)
        sig = inspect.signature(method)

        kwargs: dict[str, Any] = {}
        for param_name, param_type in hints.items():
            param = sig.parameters.get(param_name)
            has_default = param is not None and param.default is not inspect.Parameter.empty
            try:
                kwargs[param_name] = self._container._resolve_param(param_type)
            except (NoSuchBeanError, NoUniqueBeanError):
                if has_default:
                    continue
                raise NoSuchBeanError(
                    bean_type=param_type if isinstance(param_type, type) else None,
                    required_by=f"{type(config_instance).__qualname__}.{method.__name__}()",
                    parameter=f"{param_name}: {getattr(param_type, '__name__', repr(param_type))}",
                ) from None

        return method(**kwargs)

    @staticmethod
    def _sort_bean_methods(
        methods: list[tuple[str, Any]],
    ) -> list[tuple[str, Any]]:
        """Topologically sort @bean methods so dependencies are created first.

        Builds a graph where each bean's return type is a node and edges point
        from parameter types to the bean that produces them.  Falls back to the
        original order when no intra-class dependencies exist.
        """
        # Map return_type -> (attr_name, method)
        producers: dict[type, str] = {}
        method_hints: dict[str, dict[str, type]] = {}

        for attr_name, method in methods:
            hints = typing.get_type_hints(method)
            ret = hints.get("return")
            if ret is not None:
                producers[ret] = attr_name
            param_hints = {k: v for k, v in hints.items() if k != "return"}
            method_hints[attr_name] = param_hints

        # Build adjacency: attr_name -> set of attr_names it depends on
        deps: dict[str, set[str]] = {}
        for attr_name, _method in methods:
            deps[attr_name] = set()
            for _pname, ptype in method_hints.get(attr_name, {}).items():
                if ptype in producers and producers[ptype] != attr_name:
                    deps[attr_name].add(producers[ptype])

        # Kahn's algorithm
        in_degree = {name: len(d) for name, d in deps.items()}
        queue = deque(name for name, deg in in_degree.items() if deg == 0)
        ordered: list[str] = []
        while queue:
            node = queue.popleft()
            ordered.append(node)
            for name, d in deps.items():
                if node in d:
                    d.discard(node)
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        queue.append(name)

        if len(ordered) != len(methods):
            cycle_methods = [m for m in methods if m[0] not in ordered]
            logger.warning(
                "bean_method_cycle",
                extra={
                    "config": methods[0][0] if methods else "unknown",
                    "methods": [m[0] for m in cycle_methods],
                },
            )
            return methods

        name_to_entry = {attr_name: (attr_name, method) for attr_name, method in methods}
        return [name_to_entry[n] for n in ordered]

    # ------------------------------------------------------------------
    # Wiring: auto-discover and connect decorator-based beans
    # ------------------------------------------------------------------

    def _discover_post_processors(self) -> None:
        """Scan registered beans for BeanPostProcessor implementations and auto-register them."""
        count = 0
        registered_types = {type(pp) for pp in self._post_processors}
        for cls, reg in list(self._container._registrations.items()):
            if isinstance(reg.instance, BeanPostProcessor) and type(reg.instance) not in registered_types:
                self._post_processors.append(reg.instance)
                registered_types.add(type(reg.instance))
                count += 1
            elif reg.instance is None and isinstance(cls, type) and issubclass(cls, BeanPostProcessor):
                if cls in registered_types:
                    continue
                try:
                    instance = self._container.resolve(cls)
                except BeanCreationException as exc:
                    logger.debug("deferred_post_processor", extra={"bean": cls.__name__, "reason": str(exc)})
                    continue
                if type(instance) not in registered_types:
                    self._post_processors.append(instance)
                    registered_types.add(type(instance))
                    count += 1
        self._wiring_counts["post_processors"] = count
        if count:
            logger.debug("Discovered %d BeanPostProcessor(s)", count)

    @staticmethod
    def _safe_members(instance: Any, *, skip_private: bool = True) -> list[tuple[str, Any]]:
        """Return ``(name, member)`` for an instance's attributes, skipping
        ``@property`` / ``cached_property``.

        Bean wiring and lifecycle scans iterate ``dir(instance)`` to find
        decorator-marked methods. A plain ``getattr`` would evaluate property
        getters — a side-effecting or *raising* property would then corrupt or
        abort startup. Looking the attribute up statically on the class first
        avoids triggering any descriptor ``__get__``.
        """
        members: list[tuple[str, Any]] = []
        for attr_name in dir(instance):
            if skip_private and attr_name.startswith("_"):
                continue
            static_attr = inspect.getattr_static(instance, attr_name, None)
            if isinstance(static_attr, (property, functools.cached_property)):
                continue
            try:
                member = getattr(instance, attr_name)
            except Exception:
                continue
            members.append((attr_name, member))
        return members

    def _wire_app_event_listeners(self) -> None:
        """Scan singleton beans for @app_event_listener methods and subscribe to event bus."""
        count = 0
        for reg in self._container._registrations.values():
            if reg.instance is None:
                continue
            for _attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_app_event_listener__", False):
                    continue
                # Infer event type from the method's type hints
                hints = typing.get_type_hints(method)
                event_type: type[ApplicationEvent] | None = None
                for param_type in hints.values():
                    if isinstance(param_type, type) and issubclass(param_type, ApplicationEvent):
                        event_type = param_type
                        break
                if event_type is None:
                    event_type = ApplicationEvent
                self._event_bus.subscribe(event_type, method, owner_cls=type(reg.instance))
                count += 1
        self._wiring_counts["event_listeners"] = count
        if count:
            logger.debug("Wired %d @app_event_listener method(s)", count)

    def _wire_message_listeners(self) -> None:
        """Scan beans for @message_listener methods and register with MessageBrokerPort."""
        count = 0
        broker: Any | None = None
        for reg in self._container._registrations.values():
            if reg.instance is None:
                continue
            for _attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_message_listener__", False):
                    continue
                # Lazy-resolve broker on first hit
                if broker is None:
                    try:
                        from pyfly.messaging.ports.outbound import MessageBrokerPort

                        broker = self._container.resolve(MessageBrokerPort)  # type: ignore[type-abstract]
                    except BeanCreationException:
                        logger.debug("No MessageBrokerPort registered; skipping @message_listener wiring")
                        self._wiring_counts["message_listeners"] = 0
                        return
                topic = getattr(method, "__pyfly_listener_topic__", "")
                group = getattr(method, "__pyfly_listener_group__", None)
                # MessageBrokerPort.subscribe is async; defer via create_task
                task = asyncio.get_running_loop().create_task(broker.subscribe(topic, method, group=group))
                self._background_tasks.append(task)
                count += 1
        self._wiring_counts["message_listeners"] = count
        if count:
            logger.debug("Wired %d @message_listener method(s)", count)

    def _wire_cqrs_handlers(self) -> None:
        """Scan beans for @command_handler / @query_handler and register with HandlerRegistry."""
        registry: Any | None = None
        # Lazy-resolve HandlerRegistry on first decorated handler hit
        for cls, reg in self._container._registrations.items():
            if getattr(cls, "__pyfly_handler_type__", None) is None:
                continue
            if reg.instance is None:
                continue
            if registry is None:
                try:
                    from pyfly.cqrs.command.registry import HandlerRegistry

                    registry = self._container.resolve(HandlerRegistry)
                except BeanCreationException:
                    logger.debug("No HandlerRegistry registered; skipping CQRS handler wiring")
                    self._wiring_counts["cqrs_handlers"] = 0
                    return
                break

        if registry is None:
            self._wiring_counts["cqrs_handlers"] = 0
            return

        beans = [reg.instance for reg in self._container._registrations.values() if reg.instance is not None]
        registry.discover_from_beans(beans)
        count = registry.command_handler_count + registry.query_handler_count
        self._wiring_counts["cqrs_handlers"] = count
        if count:
            logger.debug("Wired %d CQRS handler(s)", count)

    def _wire_scheduled(self) -> None:
        """Discover @scheduled methods and start the TaskScheduler."""
        try:
            from pyfly.scheduling.task_scheduler import TaskScheduler
        except ImportError:
            return
        beans = [reg.instance for reg in self._container._registrations.values() if reg.instance is not None]

        # Prefer a container-managed TaskScheduler bean (from auto-config)
        scheduler = None
        for reg in self._container._registrations.values():
            if isinstance(reg.instance, TaskScheduler):
                scheduler = reg.instance
                break
        if scheduler is None:
            scheduler = TaskScheduler()

        count = scheduler.discover(beans)
        self._wiring_counts["scheduled"] = count
        if count:
            self._task_scheduler = scheduler
            task = asyncio.get_running_loop().create_task(scheduler.start())
            self._background_tasks.append(task)
            logger.debug("Discovered %d @scheduled method(s)", count)

    def _wire_async_methods(self) -> None:
        """Scan beans for @async_method and wrap them to execute in a thread pool."""
        count = 0
        for reg in self._container._registrations.values():
            if reg.instance is None:
                continue
            for attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_async__", False):
                    continue

                # Wrap the method to offload execution
                original = method

                @functools.wraps(original)
                async def async_wrapper(*args: Any, _orig: Any = original, **kwargs: Any) -> Any:
                    loop = asyncio.get_running_loop()
                    if inspect.iscoroutinefunction(_orig):
                        return await _orig(*args, **kwargs)
                    return await loop.run_in_executor(None, functools.partial(_orig, *args, **kwargs))

                setattr(reg.instance, attr_name, async_wrapper)
                count += 1
        self._wiring_counts["async_methods"] = count
        if count:
            logger.debug("Wired %d @async_method(s)", count)

    def _wire_shell_commands(self) -> None:
        """Scan @shell_component beans for @shell_method methods and register with ShellRunnerPort."""
        count = 0
        runner: Any | None = None
        for cls, reg in self._container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") != "shell_component":
                continue
            if reg.instance is None:
                continue
            # Lazy-resolve runner on first hit
            if runner is None:
                try:
                    from pyfly.shell.ports.outbound import ShellRunnerPort

                    runner = self._container.resolve(ShellRunnerPort)  # type: ignore[type-abstract]
                except BeanCreationException:
                    logger.debug("No ShellRunnerPort registered; skipping @shell_method wiring")
                    self._wiring_counts["shell_commands"] = 0
                    return
            for attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_shell_method__", False):
                    continue

                # Check @shell_method_availability
                availability_checker_name = getattr(method, "__pyfly_shell_availability__", None)
                if availability_checker_name:
                    checker = getattr(reg.instance, availability_checker_name, None)
                    if checker is not None:
                        reason = checker()
                        if reason:
                            logger.debug(
                                "Shell command '%s' unavailable: %s",
                                getattr(method, "__pyfly_shell_key__", attr_name),
                                reason,
                            )
                            continue

                from pyfly.shell.param_inference import infer_params

                key = getattr(method, "__pyfly_shell_key__", attr_name)
                help_text = getattr(method, "__pyfly_shell_help__", "")
                group = getattr(method, "__pyfly_shell_group__", "")
                params = infer_params(method)
                runner.register_command(key, method, help_text=help_text, group=group, params=params)
                count += 1
        self._wiring_counts["shell_commands"] = count
        if count:
            logger.debug("Wired %d @shell_method command(s)", count)

    async def _invoke_runners(self) -> None:
        """Invoke CommandLineRunner and ApplicationRunner beans after startup."""
        import sys

        args = sys.argv[1:]
        runners: list[tuple[int, Any]] = []
        for cls, reg in self._container._registrations.items():
            if reg.instance is None:
                continue
            if self._is_runner(reg.instance):
                runners.append((get_order(cls), reg.instance))

        runners.sort(key=lambda pair: pair[0])
        for _, runner in runners:
            hints = typing.get_type_hints(runner.run)
            hints.pop("return", None)
            first_param_type = next(iter(hints.values()), None)

            from pyfly.shell.runner import ApplicationArguments

            if first_param_type is ApplicationArguments:
                result = runner.run(ApplicationArguments.from_args(args))
            else:
                result = runner.run(args)

            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _is_runner(instance: object) -> bool:
        """Check if an instance conforms to CommandLineRunner or ApplicationRunner."""
        try:
            from pyfly.shell.ports.outbound import ShellRunnerPort
            from pyfly.shell.runner import ApplicationRunner, CommandLineRunner

            # Exclude ShellRunnerPort adapters — they satisfy CommandLineRunner
            # structurally (both have async run()) but are not lifecycle runners.
            if isinstance(instance, ShellRunnerPort):
                return False
            return isinstance(instance, (CommandLineRunner, ApplicationRunner))
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Registry stats (for startup logging)
    # ------------------------------------------------------------------

    @property
    def wiring_counts(self) -> dict[str, int]:
        """Counts from the decorator wiring phase."""
        return dict(self._wiring_counts)

    def get_bean_counts_by_stereotype(self) -> dict[str, int]:
        """Count beans grouped by stereotype (service, repository, controller, configuration)."""
        counts: dict[str, int] = {}
        for cls in self._container._registrations:
            stereotype = getattr(cls, "__pyfly_stereotype__", "other")
            counts[stereotype] = counts.get(stereotype, 0) + 1
        return counts

    async def _call_post_construct(self, instance: Any) -> None:
        """Call all @post_construct methods on an instance."""
        for attr_name, method in self._safe_members(instance, skip_private=False):
            if getattr(method, "__pyfly_post_construct__", False):
                try:
                    result = method()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    raise BeanCreationException(
                        subsystem="lifecycle",
                        provider=type(instance).__qualname__,
                        reason=f"@post_construct method '{attr_name}' failed: {exc}",
                    ) from exc

    async def _call_pre_destroy(self, instance: Any) -> None:
        """Call all @pre_destroy methods on an instance."""
        for attr_name, method in self._safe_members(instance, skip_private=False):
            if getattr(method, "__pyfly_pre_destroy__", False):
                try:
                    result = method()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.warning(
                        "pre_destroy_failed",
                        extra={
                            "bean": type(instance).__qualname__,
                            "method": attr_name,
                            "error": str(exc),
                        },
                    )
