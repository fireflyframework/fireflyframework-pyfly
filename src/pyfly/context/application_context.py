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
import dataclasses
import functools
import inspect
import logging
import types
import typing
import weakref
from collections import deque
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.container.container import Container
from pyfly.container.exceptions import (
    BeanCreationException,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.ordering import get_order
from pyfly.container.registry import Registration
from pyfly.container.types import Scope, ScopeSpec
from pyfly.context.condition_evaluator import ConditionEvaluator
from pyfly.context.environment import Environment
from pyfly.context.events import (
    ApplicationEvent,
    ApplicationEventBus,
    ApplicationEventPublisher,
    ApplicationReadyEvent,
    ContextClosedEvent,
    ContextRefreshedEvent,
)
from pyfly.context.post_processor import BeanPostProcessor
from pyfly.core.config import Config

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Why the container builds no singleton while :meth:`ApplicationContext.stop` destroys the beans.
_STOPPING = "the application context is stopping"
#: Why it builds no bean at all once the stop released them.
_STOPPED = "the application context is stopped; start it again to use its beans"


@dataclasses.dataclass(frozen=True)
class _DeferredBeanMethod:
    """A user @bean method parked until the auto-configurations have registered their beans."""

    config_instance: Any
    attr_name: str
    method: Any
    return_type: Any
    #: The one factory closure shared by the provisional and the completed registration.
    factory: Callable[[], Any]
    #: The most recent reason it could not be called; raised if it never can be.
    cause: NoSuchBeanError | NoUniqueBeanError

    def with_cause(self, cause: NoSuchBeanError | NoUniqueBeanError) -> _DeferredBeanMethod:
        return dataclasses.replace(self, cause=cause)


# The names of each class's methods marked with a lifecycle marker (``__pyfly_post_construct__``,
# ``__pyfly_pre_destroy__``), found once per class. Every bean the container creates is scanned, so
# a transient bean resolved per call must not pay a full ``dir()`` walk each time.
_MARKED_NAMES: weakref.WeakKeyDictionary[type, dict[str, tuple[str, ...]]] = weakref.WeakKeyDictionary()


def _marked_names(cls: type, marker: str) -> tuple[str, ...]:
    """The attribute names of *cls* whose function carries *marker* (properties are never evaluated)."""
    try:
        per_class = _MARKED_NAMES.setdefault(cls, {})
    except TypeError:  # a class that cannot be weakly referenced is scanned every time
        per_class = {}
    names = per_class.get(marker)
    if names is None:
        found: list[str] = []
        for attr_name in dir(cls):
            static_attr = inspect.getattr_static(cls, attr_name, None)
            if isinstance(static_attr, (property, functools.cached_property)):
                continue
            if isinstance(static_attr, (staticmethod, classmethod)):
                static_attr = static_attr.__func__
            if getattr(static_attr, marker, False):
                found.append(attr_name)
        names = per_class[marker] = tuple(found)
    return names


#: The methods a scoped ``@bean`` product that declares no other destruction is destroyed with, in the
#: order they are looked for (Spring infers ``close``/``shutdown``; ``dispose()`` is how an
#: ``AsyncEngine`` releases its pool, ``aclose()`` how an async client closes).
_INFERRED_DESTROY_METHODS: tuple[str, ...] = ("dispose", "aclose", "close")


def _takes_no_arguments(member: Any) -> bool:
    """Whether *member* can be called without arguments (a callable without a signature is assumed to)."""
    try:
        inspect.signature(member).bind()
    except TypeError:
        return False
    except ValueError:
        return True
    return True


def _marked_members(instance: Any, marker: str) -> list[tuple[str, Any]]:
    """``(name, bound member)`` for the methods of *instance* marked with *marker*.

    The member is read from the instance, so a method a BeanPostProcessor replaced on it (an
    AOP-woven wrapper) is the one called.
    """
    members: list[tuple[str, Any]] = []
    for attr_name in _marked_names(type(instance), marker):
        try:
            member = getattr(instance, attr_name)
        except Exception:  # noqa: BLE001 — a failing attribute is not a lifecycle method
            continue
        if getattr(member, marker, False):
            members.append((attr_name, member))
    return members


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
        #: Instances the container was HANDED rather than built; stop() must not release them.
        self._preexisting_instances: set[int] = set()
        #: Registration keys the last start() pipeline added; dropped at the next start so a restart
        #: rebuilds from the same registry a cold start sees.
        self._pipeline_registrations: frozenset[Any] = frozenset()
        #: The same for the (type, name) and name indexes of the container.
        self._pipeline_all: frozenset[tuple[type, str]] = frozenset()
        self._pipeline_named: frozenset[str] = frozenset()
        #: Post-processors handed to register_post_processor(). The ones discovered from beans belong
        #: to one run and are dropped by stop(), so a restart uses the new run's instances.
        self._registered_post_processors: list[BeanPostProcessor] = []
        #: The lifecycle beans this run started, in start order.
        self._lifecycle_beans: list[Any] = []
        #: Whether this run has started its lifecycle beans (step 5b): one created later is not managed.
        self._lifecycle_started = False
        #: Creation sequence of each singleton this run built, by instance identity, with the instance
        #: itself: holding it keeps its id from passing to another object during the run (an original a
        #: post-processor replaced is referenced by nothing else). A bean is always created after the
        #: beans it depends on, so reverse creation order is reverse dependency order.
        self._creation_order: dict[int, tuple[int, Any]] = {}
        #: The beans whose @app_event_listener methods this run subscribed.
        self._wired_listener_owners: list[Any] = []
        self._task_scheduler: Any | None = None
        #: User @bean methods whose parameters were not registered when their configuration was
        #: processed; completed once the auto-configurations have registered theirs.
        self._deferred_bean_methods: list[_DeferredBeanMethod] = []
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._wiring_counts: dict[str, int] = {}
        #: Non-singleton instances the container created during start() before the batched
        #: post-processing passes (step 5), which process them; ``None`` outside that window.
        self._startup_created: list[tuple[Any, Registration]] | None = None
        #: The (class, method) of each async @post_construct a synchronous creation skipped and reported.
        self._skipped_async_post_construct: set[tuple[type, str]] = set()

        # Register config and container as singleton beans (injectable like Spring's ApplicationContext)
        self._container.register_instance(Config, config)
        self._container.register_instance(Container, self._container)
        # Injectable event publisher (Spring ApplicationEventPublisher) — beans can fire
        # lifecycle or arbitrary domain events into the bus.
        self._container.register_instance(ApplicationEventPublisher, ApplicationEventPublisher(self._event_bus))
        # Built-in "refresh" scope + injectable ContextRefresher (Spring Cloud @RefreshScope).
        from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME, RefreshScope
        from pyfly.context.refresh import ContextRefresher

        refresh_scope = RefreshScope()
        self._container.register_scope(REFRESH_SCOPE_NAME, refresh_scope)
        self._container.register_instance(
            ContextRefresher,
            ContextRefresher(
                self._container, refresh_scope, self._event_bus, self._config, destroy=self._destroy_scoped_instance
            ),
        )

    # ------------------------------------------------------------------
    # Bean registration
    # ------------------------------------------------------------------

    def register_bean(self, cls: type, **kwargs: Any) -> None:
        """Register a bean class with the context."""
        name: str = kwargs.get("name", "") or getattr(cls, "__pyfly_bean_name__", "")
        scope: Scope = kwargs.get("scope") or getattr(cls, "__pyfly_scope__", Scope.SINGLETON)  # type: ignore[assignment]
        self._container.register(cls, scope=scope, name=name)

    def register_post_processor(self, processor: BeanPostProcessor) -> None:
        """Register a BeanPostProcessor (kept across restarts, unlike the ones discovered from beans)."""
        self._registered_post_processors.append(processor)
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
        """Start the context: resolve @configuration beans, call lifecycle hooks, publish events.

        Idempotent. A context that is already started returns immediately, because re-running the
        pipeline does not refresh the context — it BUILDS A SECOND ONE beside it. Auto-configurations
        register again, ``@configuration`` classes are processed again, and a second fully-initialised
        set of singletons is created and started: a second Kafka consumer joins the same group and
        steals partitions from the first, a second scheduler fires every ``@scheduled`` task twice, a
        second connection pool opens. Nothing owns the duplicates, so :meth:`stop` disposes one set and
        leaks the other.

        A double start is easy to reach — an ASGI server that runs the lifespan twice, a reload, a test
        harness sharing one module-level application across files — and it fails silently, which is the
        worst property a lifecycle bug can have. The context therefore guards itself, as the event
        buses do (``KafkaEventBus.start()`` opens with ``if self._started: return``); within one start
        each lifecycle bean is started once, however many types it is registered under.

        Lifecycle beans (any singleton whose class defines ``start()`` and ``stop()``) start in
        ascending :func:`~pyfly.kernel.lifecycle.lifecycle_phase` and, within a phase, in creation
        order, so a bean starts after the beans it depends on (a user bean that writes on start runs
        after ``ddl-auto`` and the migrations). The ``@bean`` products start before the eager
        singletons are created, to fail fast on connectivity; the lifecycle beans created after that
        (a scanned ``@component``) start once every singleton is initialized.

        :meth:`stop` clears the flag, so a stopped context can be started again and rebuilds normally.
        """
        if self._started:
            logger.debug("context_start_ignored", extra={"reason": "already started"})
            return

        try:
            await self._do_start()
        except BeanCreationException:
            self._startup_created = None
            raise
        except Exception as exc:
            self._startup_created = None
            raise BeanCreationException(
                subsystem="startup",
                provider=type(exc).__qualname__,
                reason=str(exc),
            ) from exc

    async def _do_start(self) -> None:
        """Internal startup logic."""
        # A RESTART MUST REPRODUCE A COLD START.
        #
        # stop() releases the singletons it built, but the REGISTRATIONS this pipeline created are
        # still here, and layering a second pipeline on top of them is not a rebuild. Two things go
        # wrong, both silent. A @bean reachable under several keys — its concrete class and the
        # protocol it satisfies — stops being one object, because the first start aliased those keys to
        # a single instance and a second pass resolves each key independently, calling the factory once
        # per key. And the registry drifts, because the conditional passes re-evaluate against a
        # registry that already holds the previous run's output rather than against the user's own
        # definitions. Dropping what the last pipeline added puts the registry back to the state a cold
        # start begins from.
        for key in getattr(self, "_pipeline_registrations", frozenset()):
            self._container._registrations.pop(key, None)
        for all_key in self._pipeline_all:
            if self._container._all.pop(all_key, None) is not None:
                self._container._unindex_name(*all_key)
        for name in self._pipeline_named:
            self._container._named.pop(name, None)
        self._container.allow_creation()

        registrations_before = set(self._container._registrations.keys())
        all_before = set(self._container._all.keys())
        named_before = set(self._container._named.keys())

        # Every instance the container creates from now on goes through the init pipeline. Until
        # the batched passes of step 5 run, the hook only collects the non-singleton ones (the
        # singletons are on their registrations) so that step 5 processes each once.
        self._startup_created = []
        self._lifecycle_started = False
        self._container._post_create_hook = self._on_bean_created

        # Whatever already carries an instance was HANDED to the container, not built by it — the
        # container's self-registration, the context's own, anything an embedder registered as a
        # ready-made object. stop() releases what this start creates and leaves these alone, because
        # they cannot be rebuilt: they have no factory, and discarding them breaks the next start.
        self._preexisting_instances = {
            id(reg.instance)
            for reg in self._container._registrations.values()
            if getattr(reg, "instance", None) is not None
        }

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

        # 2d. Complete the user @bean methods that were waiting on an auto-configured bean.
        self._process_deferred_bean_methods()

        # 2e. Start the lifecycle beans the @bean methods produced (fail-fast: validates connectivity)
        await self._start_lifecycle_beans()

        # 3. Auto-discover BeanPostProcessors from registered beans
        self._discover_post_processors()

        # 3b. Bind @config_properties beans from config (audit #118)
        self._bind_config_properties()

        # 4. Eagerly resolve all singletons (sorted by @order)
        sorted_entries = sorted(
            self._container._registrations.items(),
            key=lambda item: get_order(item[0]),
        )
        for cls, reg in sorted_entries:
            # @lazy beans are not eagerly created — they resolve on first request.
            if reg.scope == Scope.SINGLETON and reg.instance is None and not getattr(cls, "__pyfly_lazy__", False):
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

        # The non-singleton instances created so far (a TRANSIENT repository injected into a
        # singleton, the transient session a @bean received) join the batch. From here on the
        # post-create hook runs the pipeline itself, so a bean first created from now on (a @lazy
        # singleton resolved by a @post_construct, an event listener or a runner, or any scoped
        # bean) is post-processed when it is created.
        scoped_instances = self._startup_created or []
        self._startup_created = None

        # An interface-typed @bean is registered under both its concrete and
        # return type, so the same instance appears in two Registration entries.
        # Group registrations by instance identity and process each unique
        # instance exactly once, propagating the (possibly AOP-wrapped) result
        # back to every alias — otherwise @post_construct/BeanPostProcessors run
        # twice and one alias keeps the un-woven object (audit #113). A scoped
        # instance has no alias: nothing caches it on a registration.
        units: list[tuple[str, Any, list[Registration], ScopeSpec]] = []
        groups_by_id: dict[int, list[Registration]] = {}
        for reg in self._all_registrations():
            if reg.instance is None:
                continue
            grp = groups_by_id.get(id(reg.instance))
            if grp is None:
                grp = []
                groups_by_id[id(reg.instance)] = grp
                units.append((reg.display_name, reg.instance, grp, Scope.SINGLETON))
            grp.append(reg)
        for instance, reg in scoped_instances:
            if id(instance) not in groups_by_id:
                groups_by_id[id(instance)] = []
                units.append((reg.display_name, instance, [], reg.scope))

        # Pass 1: before_init for every bean (collects all @aspect beans, etc.)
        for index, (bean_name, inst, aliases, scope) in enumerate(units):
            for pp in self._post_processors_for(sorted_pps, scope):
                inst = pp.before_init(inst, bean_name)
            for member in aliases:
                member.instance = inst
            units[index] = (bean_name, inst, aliases, scope)

        # Pass 2: @post_construct then after_init (weaving now sees every aspect)
        for bean_name, inst, aliases, scope in units:
            await self._call_post_construct(inst)
            for pp in self._post_processors_for(sorted_pps, scope):
                inst = pp.after_init(inst, bean_name)
            for member in aliases:
                if member.instance is not inst:
                    self._carry_creation_order(member.instance, inst)
                member.instance = inst
            self._report_skipped_post_processors(sorted_pps, inst, bean_name, scope)

        # 5b. Start the lifecycle beans that did not exist at step 2e (scanned stereotypes, beans a
        # @bean did not pull in). They used to be neither started nor stopped.
        await self._start_lifecycle_beans()
        self._lifecycle_started = True

        # 6. Wire decorator-based beans to their targets
        self._wire_app_event_listeners()
        self._wire_message_listeners()
        self._wire_event_listeners()
        self._wire_cqrs_handlers()
        self._wire_scheduled()
        self._wire_async_methods()
        self._wire_shell_commands()

        # 7. Publish lifecycle events
        await self._event_bus.publish(ContextRefreshedEvent())
        await self._event_bus.publish(ApplicationReadyEvent())
        await self._invoke_runners()
        # Everything this pipeline added, so the next start can drop it and begin from the same
        # registry a cold start begins from.
        self._pipeline_registrations = frozenset(self._container._registrations.keys()) - registrations_before
        self._pipeline_all = frozenset(self._container._all.keys()) - all_before
        self._pipeline_named = frozenset(self._container._named.keys()) - named_before

        self._started = True

    async def stop(self) -> None:
        """Stop the context: drain, destroy, release — the database last.

        The steps, each bounded per bean by ``pyfly.context.shutdown-timeout`` (30 s by default; a bean
        that exceeds it is logged and skipped so it cannot block the rest):

        1. ``ContextClosedEvent`` is published while every bean still works (Spring publishes it
           first too);
        2. the drain: background tasks are cancelled, the ``TaskScheduler`` stops (it waits for the
           jobs in flight), and the consumer-phase lifecycle beans stop (event buses, message
           brokers: :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`), so no new work arrives;
        3. from here on the container builds no singleton (:class:`BeanCreationNotAllowedError`), but
           still builds transient and scoped beans; every singleton gets its ``@pre_destroy``, each
           bean before the beans it depends on, so a ``@pre_destroy`` can still write through a
           ``Provider[AsyncSession]`` or a proxied refresh-scoped datasource; then the instances the
           custom scopes hold (refresh-scoped beans) are destroyed;
        4. the other lifecycle beans stop, highest phase first and in reverse start order within a
           phase: they own what the destroyed beans used (clients, ``create-drop`` schema). A scoped
           instance one of them built while stopping is destroyed after them, and from then on no
           scoped instance is built either. Then the singleton ``@bean`` products get their declared
           ``destroy_method`` (an engine's ``dispose()``), in the order of step 3: a lifecycle bean
           may still have used them in its ``stop()``;
        5. every :class:`~pyfly.kernel.lifecycle.ResourceRegistry` bean is disposed: the datasource
           registry closes every engine, last;
        6. the singletons this run built are released, and everything the run added (lifecycle
           beans, discovered post-processors, event listeners, the post-create hook) is reset, so a
           restart is a cold start. From here on the container builds no bean of any scope.

        Until 26.09.07 the adapters stopped first, in reverse registration order: the primary engine
        was disposed before the consumers, the user lifecycle beans and every ``@pre_destroy``, and
        the writes that followed reconnected through a pool nobody disposed.
        """
        from pyfly.kernel.lifecycle import deferring_disposal, is_resource_registry

        shutdown_timeout = float(self._config.get("pyfly.context.shutdown-timeout", 30))
        # The resource registries are disposed in step 5 and nowhere earlier: a lifecycle bean that
        # closes one on stop (the datasource registry's) would otherwise close it at its own place in
        # the order, before the beans that still use it, whatever the registration order was.
        registries = [
            reg.instance
            for reg in self._all_registrations()
            if reg.instance is not None and is_resource_registry(reg.instance)
        ]
        with deferring_disposal(registries):
            live = await self._drain_destroy_and_stop(shutdown_timeout)

        # 5. The resource registries (the datasource registry), last.
        disposed: set[int] = set()
        for instance in live:
            if not is_resource_registry(instance) or id(instance) in disposed:
                continue
            disposed.add(id(instance))
            try:
                await asyncio.wait_for(instance.dispose_all(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.warning(
                    "resource_registry_dispose_timeout",
                    extra={"bean": type(instance).__qualname__, "timeout_s": shutdown_timeout},
                )
            except Exception:
                logger.warning(
                    "resource_registry_dispose_failed", extra={"bean": type(instance).__qualname__}, exc_info=True
                )

        self._release_run(live)

    async def _drain_destroy_and_stop(self, shutdown_timeout: float) -> list[Any]:
        """Steps 1 to 4 of :meth:`stop`; returns every singleton, in the order they were destroyed."""
        from pyfly.kernel.lifecycle import CONSUMER_PHASE, lifecycle_phase

        # 1. Tell the application first, while it still works.
        try:
            await asyncio.wait_for(self._event_bus.publish(ContextClosedEvent()), timeout=shutdown_timeout)
        except TimeoutError:
            logger.warning("context_closed_event_timeout", extra={"timeout_s": shutdown_timeout})
        except Exception:
            logger.warning("context_closed_event_failed", exc_info=True)

        # 2. Drain: nothing dispatches new work into the beans about to be destroyed.
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        stopped: set[int] = set()
        if self._task_scheduler is not None:
            stopped.add(id(self._task_scheduler))
            try:
                await asyncio.wait_for(self._task_scheduler.stop(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.warning("task_scheduler_stop_timeout", extra={"timeout_s": shutdown_timeout})
            except Exception:
                logger.debug("task_scheduler_stop_failed", exc_info=True)

        stop_order = self._lifecycle_stop_order()
        for bean in stop_order:
            if lifecycle_phase(bean) >= CONSUMER_PHASE and id(bean) not in stopped:
                stopped.add(id(bean))
                await self._stop_lifecycle_bean(bean, shutdown_timeout)

        # 3. Destroy the singletons, each before the beans it depends on. From here on no SINGLETON is
        # created: one resolved now would outlive the stop. A transient or scoped bean still is (Spring
        # refuses only singletons while they are destroyed), so a @pre_destroy can still write through
        # a Provider[AsyncSession] or a proxied refresh-scoped datasource.
        self._container.refuse_creation(_STOPPING, scopes=(Scope.SINGLETON,))
        live = self._live_instances_in_destroy_order()
        for instance in live:
            await self._pre_destroy_instance(instance, shutdown_timeout)

        # 3b. The instances the custom scopes hold (refresh-scoped datasources), now that the singletons
        # that used them through a proxy or a Provider are done with them.
        await self._destroy_scoped_instances(shutdown_timeout)

        # 4. The lifecycle beans that own what the destroyed beans used.
        for bean in stop_order:
            if id(bean) not in stopped:
                stopped.add(id(bean))
                await self._stop_lifecycle_bean(bean, shutdown_timeout)

        # 4b. A scoped instance a lifecycle bean built while it stopped is destroyed as well. After this
        # no scoped instance is created either: nothing would destroy it.
        await self._destroy_scoped_instances(shutdown_timeout)
        self._container.refuse_creation(_STOPPING, scopes=(Scope.SINGLETON, *self._container._custom_scopes))

        # 4c. The destroy methods of the singleton @bean products (an engine's dispose(), a client's
        # close()), in the destroy order: they release what the lifecycle beans may still have used in
        # their stop(), which would otherwise reopen a pool nobody disposes again.
        declared = {
            id(reg.instance): reg.destroy_method
            for reg in self._all_registrations()
            if reg.instance is not None and reg.destroy_method is not None
        }
        for instance in live:
            if id(instance) in declared:
                await self._call_destroy_method(instance, declared[id(instance)], shutdown_timeout, infer=False)
        return live

    async def _destroy_scoped_instances(self, timeout: float) -> None:
        """Evict every instance the custom scopes hold and destroy each, the most recently created first.

        A scoped bean is cached after the scoped beans it depends on, so the reverse cache order destroys
        each before its dependencies. Only a scope whose handler offers ``evict_all()`` can be emptied.
        """
        for handler in list(self._container._custom_scopes.values()):
            evict_all = getattr(handler, "evict_all", None)
            if not callable(evict_all):
                continue
            for key, scoped in reversed(list(evict_all().items())):
                await self._destroy_scoped_instance(key, scoped, timeout=timeout)

    def _release_run(self, live: list[Any]) -> None:
        """Step 6 of :meth:`stop`: release the destroyed singletons and forget what the run added."""
        # 6. RELEASE what was just destroyed. @pre_destroy has closed these pools, stopped these
        # consumers and flushed these files, so keeping them on their registrations leaves the
        # container handing out objects that no longer work: get_bean() after a stop returned a
        # destroyed singleton instead of failing or rebuilding. It also made a restart accumulate —
        # start() creates a fresh set BESIDE the stale one, so anything walking the registrations
        # (health reporting, metrics, a bean inventory) sees every singleton twice, one live and one
        # dead. Clearing here is what makes stop() the inverse of start() rather than half of it.
        destroyed = {id(instance) for instance in live}
        released = 0
        for reg in self._all_registrations():
            if (
                reg.instance is not None
                and id(reg.instance) in destroyed
                and id(reg.instance) not in self._preexisting_instances
            ):
                reg.instance = None
                released += 1

        if released:
            logger.debug(
                "context_singletons_released",
                extra={"registrations": released, "instances": len(destroyed)},
            )

        # A RESTART MUST REPRODUCE A COLD START: forget everything this run added besides the
        # singletons, or the next start restarts the stopped adapters, post-processes with the
        # previous run's post-processors and delivers each event to the previous run's listeners too.
        self._event_bus.unsubscribe_owners(self._wired_listener_owners)
        self._wired_listener_owners = []
        self._lifecycle_beans = []
        self._lifecycle_started = False
        self._post_processors = list(self._registered_post_processors)
        self._container._post_create_hook = None
        # The proxies handed to the released singletons; the next run's registrations get their own.
        self._container._scoped_proxies.clear()
        self._task_scheduler = None
        self._creation_order.clear()
        self._wiring_counts = {}
        self._container.refuse_creation(_STOPPED)
        self._started = False

    async def _destroy_scoped_instance(self, key: str, instance: Any, *, timeout: float | None = None) -> None:
        """Destroy *instance*, which its scope evicted (a refresh) or still held at stop under *key*.

        It gets the whole destruction contract, each step bounded by the shutdown timeout: its
        ``@pre_destroy`` methods; then the destroy method of the ``@bean`` that produced it (its
        ``destroy_method``, or when it declares no other destruction the inferred ``dispose()``,
        ``aclose()`` or ``close()``: the documented refresh-scoped ``AsyncEngine`` bean disposes its
        pool this way); then ``stop()`` when it is a lifecycle bean. The context does not start a
        scoped lifecycle bean (the scope builds it on demand, in a synchronous resolution), but its
        ``stop()`` is how it releases what it holds.
        """
        limit = float(self._config.get("pyfly.context.shutdown-timeout", 30)) if timeout is None else timeout
        registration = self._scoped_registration(key)
        declared = registration.destroy_method if registration is not None else None
        await self._destroy_instance(instance, declared, limit, infer=True)
        if self._has_lifecycle_methods(instance):
            await self._stop_lifecycle_bean(instance, limit)

    def _scoped_registration(self, key: str) -> Registration | None:
        """The scoped registration whose instances a scope caches under *key*."""
        from pyfly.container.container import scope_key

        for reg in self._all_registrations():
            if reg.scope != Scope.SINGLETON and scope_key(reg) == key:
                return reg
        return None

    async def _destroy_instance(self, instance: Any, declared: str | None, timeout: float, *, infer: bool) -> None:
        """Run the ``@pre_destroy`` methods of *instance*, then its destroy method (see :func:`~pyfly.container.bean`).

        *declared* is the ``destroy_method`` of the ``@bean`` that produced it (``None`` for any other
        bean). The inferred one is looked for only when *infer* is true: for a scoped instance, never
        for a singleton (whose two halves ``stop()`` runs at different steps). Each call is bounded by
        *timeout*; a failure is logged and does not stop the destruction of the others.
        """
        await self._pre_destroy_instance(instance, timeout)
        await self._call_destroy_method(instance, declared, timeout, infer=infer)

    async def _pre_destroy_instance(self, instance: Any, timeout: float) -> None:
        """Run the ``@pre_destroy`` methods of *instance* within *timeout*."""
        try:
            await asyncio.wait_for(self._call_pre_destroy(instance), timeout=timeout)
        except TimeoutError:
            logger.warning("pre_destroy_timeout", extra={"bean": type(instance).__qualname__, "timeout_s": timeout})

    async def _call_destroy_method(self, instance: Any, declared: str | None, timeout: float, *, infer: bool) -> None:
        """Call the destroy method of *instance* (see :meth:`_destroy_instance`) within *timeout*."""
        name = type(instance).__qualname__
        method_name = self._destroy_method_name(instance, declared, infer=infer)
        if method_name is None:
            return
        try:
            result = getattr(instance, method_name)()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except TimeoutError:
            logger.warning("destroy_method_timeout", extra={"bean": name, "method": method_name, "timeout_s": timeout})
        except Exception:
            logger.warning("destroy_method_failed", extra={"bean": name, "method": method_name}, exc_info=True)

    def _destroy_method_name(self, instance: Any, declared: str | None, *, infer: bool) -> str | None:
        """The name of the method that destroys *instance*, or ``None`` when it has none."""
        from pyfly.container.bean import INFER_DESTROY_METHOD

        if not declared:
            return None
        if declared != INFER_DESTROY_METHOD:
            if not callable(getattr(instance, declared, None)):
                logger.warning(
                    "destroy_method_missing", extra={"bean": type(instance).__qualname__, "method": declared}
                )
                return None
            return declared
        if not infer or _marked_names(type(instance), "__pyfly_pre_destroy__") or self._has_lifecycle_methods(instance):
            return None
        for candidate in _INFERRED_DESTROY_METHODS:
            member = getattr(instance, candidate, None)
            if callable(member) and _takes_no_arguments(member):
                return candidate
        return None

    async def _stop_lifecycle_bean(self, bean: Any, timeout: float) -> None:
        """Stop one lifecycle bean within *timeout*; a failure is logged and does not stop the others."""
        name = type(bean).__qualname__
        try:
            await asyncio.wait_for(bean.stop(), timeout=timeout)
        except TimeoutError:
            logger.warning("adapter_stop_timeout", extra={"adapter": name, "timeout_s": timeout})
        except Exception:
            logger.warning("adapter_stop_failed", extra={"adapter": name}, exc_info=True)

    def _lifecycle_stop_order(self) -> list[Any]:
        """The started lifecycle beans, highest phase first and in reverse start order within a phase."""
        from pyfly.kernel.lifecycle import lifecycle_phase

        indexed = list(enumerate(self._lifecycle_beans))
        indexed.sort(key=lambda item: (-lifecycle_phase(item[1]), -item[0]))
        return [bean for _, bean in indexed]

    def _live_instances_in_destroy_order(self) -> list[Any]:
        """Every singleton instance, once, the most recently created first.

        A bean is created after the beans it depends on (they are its constructor or factory
        arguments), so this destroys each bean before its dependencies. Instances the context did not
        build (the container itself, ``Config``, embedder-registered objects) come last.
        """
        instances: list[Any] = []
        seen: set[int] = set()
        for reg in self._all_registrations():
            if reg.instance is not None and id(reg.instance) not in seen:
                seen.add(id(reg.instance))
                instances.append(reg.instance)
        position = {id(instance): index for index, instance in enumerate(instances)}
        instances.sort(
            key=lambda instance: (self._creation_index(instance), position[id(instance)]),
            reverse=True,
        )
        return instances

    def _all_registrations(self) -> list[Registration]:
        """Every registration the container holds, once: the by-type slots, the named and the typed indexes.

        Two beans of one concrete type share a by-type slot, so the slots alone miss one of them.
        """
        out: list[Registration] = []
        seen: set[int] = set()
        container = self._container
        for reg in (*container._registrations.values(), *container._all.values(), *container._named.values()):
            if id(reg) not in seen:
                seen.add(id(reg))
                out.append(reg)
        return out

    def _note_created(self, instance: Any) -> None:
        """Record that singleton *instance* now exists (its place in the destroy order)."""
        if id(instance) not in self._creation_order:
            self._creation_order[id(instance)] = (len(self._creation_order), instance)

    def _carry_creation_order(self, previous: Any, replacement: Any) -> None:
        """A post-processor replaced *previous* with *replacement*: it keeps the original's place."""
        entry = self._creation_order.get(id(previous))
        if entry is not None:
            self._creation_order.setdefault(id(replacement), (entry[0], replacement))

    def _creation_index(self, instance: Any) -> int:
        """The place of *instance* in this run's creation order, or ``-1`` when the run did not build it."""
        entry = self._creation_order.get(id(instance))
        return -1 if entry is None else entry[0]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register_auto_configurations(self) -> None:
        """Register built-in @auto_configuration classes for condition evaluation."""
        from pyfly.config.auto import discover_auto_configurations

        for cls in discover_auto_configurations():
            if cls not in self._container._registrations:
                self.register_bean(cls)

    async def _start_lifecycle_beans(self) -> None:
        """Start the lifecycle beans not started yet, by phase and then creation order.

        A bean registered under several types (a ``@bean`` declared as a port) is one instance and is
        started once; so is a bean whose instance appears under several registrations.
        """
        from pyfly.kernel.lifecycle import lifecycle_phase

        started = {id(bean) for bean in self._lifecycle_beans}
        candidates: list[Any] = []
        for reg in self._all_registrations():
            instance = reg.instance
            if instance is None or id(instance) in started:
                continue
            started.add(id(instance))
            if self._has_lifecycle_methods(instance):
                candidates.append(instance)
        candidates.sort(key=lambda bean: (lifecycle_phase(bean), self._creation_index(bean)))

        for adapter in candidates:
            try:
                await adapter.start()
            except Exception as exc:
                raise BeanCreationException(
                    subsystem=self._infer_subsystem(adapter),
                    provider=type(adapter).__name__,
                    reason=str(exc),
                ) from exc
            self._lifecycle_beans.append(adapter)

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
        """Remove a bean registration from every index: by type, by name, and by ``(type, name)``."""
        reg = self._container._registrations.pop(cls)
        if reg.name and reg.name in self._container._named:
            del self._container._named[reg.name]
        if self._container._all.get((cls, reg.name)) is reg:
            del self._container._all[(cls, reg.name)]
            self._container._unindex_name(cls, reg.name)

    @staticmethod
    def _declared_bean_type(return_type: Any) -> type | None:
        """The one class a ``@bean`` return hint declares, or ``None`` when it declares none.

        A bare class is itself. ``Port | None`` / ``Optional[Port]`` — the idiomatic hint for
        a factory that may decline — declares ``Port``: that is what a dependant injects and
        what ``@conditional_on_missing_bean(Port)`` asks about, so the deferred pass must claim
        it provisionally under ``Port`` or the fallback auto-configuration registers a second
        bean for the same port in the window before the user's factory runs. ``A | B`` with two
        classes declares no single port and gets no claim: the container cannot know which one
        the user means, and guessing would silence a fallback the user may rely on. A union is
        never itself a registration key (it has no ``__name__`` and crashed the admin beans
        graph); only the unwrapped class is.
        """
        if isinstance(return_type, type):
            return return_type
        if typing.get_origin(return_type) in (typing.Union, types.UnionType):
            members = [arg for arg in typing.get_args(return_type) if arg is not type(None)]
            if len(members) == 1 and isinstance(members[0], type):
                return members[0]
        return None

    @classmethod
    def _declared_scoped_bean_type(cls, return_type: Any) -> type | None:
        """The class a non-singleton ``@bean`` is registered under without calling it.

        As :meth:`_declared_bean_type`, and a parametrized generic declares its origin class:
        ``-> async_sessionmaker[AsyncSession]`` (the idiomatic hint), and ``... | None``, declare
        ``async_sessionmaker``. That is what an injection of the parametrized type resolves (the
        container falls back to the origin), so the factory need not run at startup to be found. A
        builtin container (``list[X]``, ``dict[K, V]``) or ``type[X]`` declares nothing: those are not
        bean keys. Singletons keep :meth:`_declared_bean_type`, since their factory runs anyway and
        registers the concrete class it returns.
        """
        declared = cls._declared_bean_type(return_type)
        if declared is not None:
            return declared
        hint = return_type
        if typing.get_origin(hint) in (typing.Union, types.UnionType):
            members = [arg for arg in typing.get_args(hint) if arg is not type(None)]
            if len(members) != 1:
                return None
            hint = members[0]
        origin = typing.get_origin(hint)
        if isinstance(origin, type) and origin not in (list, dict, set, frozenset, tuple, type):
            return origin
        return None

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
                profile_expr = getattr(method, "__pyfly_profile__", "")
                if profile_expr and not self._environment.accepts_profiles(profile_expr):
                    continue
                bean_methods.append((attr_name, method))

            bean_methods = self._sort_bean_methods(bean_methods)

            for attr_name, method in bean_methods:
                # Get return type from method hints
                hints = typing.get_type_hints(method)
                return_type = hints.get("return")
                if return_type is None:
                    continue

                # A non-singleton bean is built when its scope asks for an instance, never here: a
                # REQUEST-scoped factory has no request at startup (one that reads the request or
                # takes another request-scoped bean failed start()), and the product of a
                # refresh-scoped or transient factory was thrown away (an extra engine per scoped
                # datasource). The declared return class is the registration key, as in Spring.
                if self._register_scoped_bean_method(config_instance, attr_name, method, return_type):
                    continue

                # Resolve the factory's parameters from the container. USER configurations are
                # processed before AUTO-configurations so that @conditional_on_missing_bean can
                # see what the user declared — but that means a user factory taking an
                # auto-configured bean (async_sessionmaker, EventPublisher, a client pool) finds
                # nothing registered yet. Failing here made the framework offer a bean and refuse
                # to inject it, and every application rebuilt the object by hand. So a user
                # factory whose parameters are not there yet is DEFERRED: its declared return
                # type is registered now (so the missing-bean conditions still back off, and so a
                # resolve in the meantime builds it lazily through the factory) and the call is
                # completed in step 2d, once the auto-configurations have registered theirs.
                # Parameters are resolved BEFORE the method runs, so deferring never re-runs a
                # factory body that already started.
                #
                # An auto-configuration is never deferred: it is processed last, so a dependency
                # it cannot resolve will not appear later, and raising now keeps the error at the
                # bean that declared it.
                try:
                    kwargs = self._bean_method_kwargs(config_instance, method)
                except (NoSuchBeanError, NoUniqueBeanError) as exc:
                    # An ambiguous parameter is deferred too: an auto-configuration may register the
                    # @primary candidate that settles it.
                    if auto:
                        raise
                    self._defer_bean_method(config_instance, attr_name, method, return_type, cause=exc)
                    continue

                result = method(**kwargs)
                self._register_bean_result(config_instance, attr_name, method, return_type, result)

    def _register_scoped_bean_method(self, config_instance: Any, attr_name: str, method: Any, return_type: Any) -> bool:
        """Register a non-singleton ``@bean`` method under its declared class without calling it.

        Returns ``False`` for a singleton, and for a non-singleton whose return hint declares no single
        class (``A | B``, ``list[X]``): that one is still called once at startup to learn its concrete
        type. A parametrized hint (``-> async_sessionmaker[AsyncSession]``) declares its origin class.
        """
        bean_scope = getattr(method, "__pyfly_bean_scope__", Scope.SINGLETON)
        if bean_scope == Scope.SINGLETON:
            return False
        declared = self._declared_scoped_bean_type(return_type)
        if declared is None:
            return False
        bean_name = getattr(method, "__pyfly_bean_name__", "") or attr_name
        self._container.register(declared, scope=bean_scope, name=bean_name)
        registration = self._container._registrations[declared]
        registration.scope = bean_scope  # the method's scope, even when the class carries a stereotype's
        registration.factory = self._bean_factory(config_instance, method)
        registration.scoped_proxy = bool(getattr(method, "__pyfly_scoped_proxy__", False))
        registration.destroy_method = self._declared_destroy_method(method)
        if getattr(method, "__pyfly_bean_primary__", False):
            registration.primary = True
        return True

    def _register_bean_result(
        self,
        config_instance: Any,
        attr_name: str,
        method: Any,
        return_type: Any,
        result: Any,
        *,
        factory: Callable[[], Any] | None = None,
    ) -> None:
        """Register the object a @bean method produced under its concrete and declared types.

        ``factory`` is the closure a deferred bean already registered provisionally; reusing it
        keeps the provisional registration and the completed one identical for everything that
        de-duplicates by factory identity (the single-candidate condition, the lifecycle passes).
        """
        bean_name = getattr(method, "__pyfly_bean_name__", "") or attr_name
        bean_scope = getattr(method, "__pyfly_bean_scope__", Scope.SINGLETON)

        if result is None:
            # The factory declined (``-> Port | None`` answering ``None``). Nothing is
            # registered — least of all ``NoneType``, which is what ``type(result)`` would have
            # keyed — and a provisional claim a deferred factory made must go with it, or a
            # later resolve of ``Port`` would run the factory again and hand out ``None``.
            declared = self._declared_bean_type(return_type)
            if declared is not None:
                reg = self._container._registrations.get(declared)
                if reg is not None and reg.factory is factory and reg.instance is None:
                    self._remove_registration(declared)
            return

        # Register bean: use the concrete type so multiple beans
        # returning the same interface type don't overwrite each other.
        # A factory closure is stored so TRANSIENT beans rebuild through
        # the @bean method (not __init__) on each resolution.
        impl_type = type(result)
        if factory is None:
            factory = self._bean_factory(config_instance, method)
        self._container.register(impl_type, scope=bean_scope, name=bean_name)
        impl_reg = self._container._registrations[impl_type]
        impl_reg.factory = factory
        impl_reg.destroy_method = self._declared_destroy_method(method)
        if getattr(method, "__pyfly_bean_primary__", False):
            impl_reg.primary = True
        if bean_scope == Scope.SINGLETON:
            impl_reg.instance = result
            self._note_created(result)

        # The type the hint DECLARES. A PEP 604 union / TypeVar / generic-alias
        # return hint is *not* a real class: ``get_type_hints`` preserves
        # ``Foo | None`` as a ``types.UnionType``, which has no
        # ``__name__``/``__qualname__``/``__module__``. It must never become a
        # ``_registrations`` or ``_bindings`` key (a non-class key crashed the
        # admin BeansProvider, 500 on ``/admin/api/beans/graph``) — but the
        # class inside it is what the user meant: ``-> Port | None`` returning a
        # ``UserPort`` must make ``get_bean(Port)`` answer, exactly as ``-> Port``
        # does, and must complete the provisional ``Port`` registration a deferred
        # factory claimed. Until 26.09.06 the union was bound as-is, so ``Port``
        # resolved for nobody and the deferred claim was never completed (the
        # factory ran a second time on the next resolve). A two-class union
        # declares nothing; the concrete ``impl_type`` registered above still
        # backs resolution by the concrete type.
        declared = self._declared_bean_type(return_type)

        # Bind declared type → concrete type for list[T] resolution
        if declared is not None and declared is not impl_type:
            self._container.bind(declared, impl_type)

        # Also keep a direct registration for the declared type
        # (for single-bean resolution) unless it already exists. It
        # shares the same instance/factory; the startup lifecycle, wiring
        # and destroy passes de-duplicate by instance identity so the bean is
        # never post-processed, subscribed, started, stopped or destroyed twice
        # (audit #113, C102).
        if declared is not None and declared not in self._container._registrations:
            self._container.register(declared, scope=bean_scope)
            return_reg = self._container._registrations[declared]
            return_reg.factory = factory
            return_reg.destroy_method = self._declared_destroy_method(method)
            if bean_scope == Scope.SINGLETON:
                return_reg.instance = result
        elif declared is not None and (
            getattr(method, "__pyfly_bean_primary__", False)
            or self._container._registrations[declared].factory is factory
        ):
            # A later @bean(primary=True) for the same return type must win the
            # single-bean direct resolution (the @Bean @Primary semantics) —
            # otherwise resolve() returns whichever @bean was processed first.
            # The same completion applies to a deferred bean's own provisional
            # registration (recognised by its factory), which is now given the
            # instance it was standing in for.
            return_reg = self._container._registrations[declared]
            return_reg.factory = factory
            return_reg.destroy_method = self._declared_destroy_method(method)
            if getattr(method, "__pyfly_bean_primary__", False):
                return_reg.primary = True
            if bean_scope == Scope.SINGLETON:
                return_reg.instance = result

    def _defer_bean_method(
        self,
        config_instance: Any,
        attr_name: str,
        method: Any,
        return_type: Any,
        *,
        cause: NoSuchBeanError | NoUniqueBeanError,
    ) -> None:
        """Park a user @bean method until the auto-configurations have registered their beans.

        The declared return type is registered right away, with the factory closure and no
        instance, for two reasons. ``@conditional_on_missing_bean(ReturnType)`` on an
        auto-configuration is evaluated between now and the deferred pass, and it must still
        see the user's declaration — otherwise deferral would hand the application two beans for
        one port, the user's and the fallback it meant to replace. And anything that resolves the
        type before the deferred pass (an auto-configured bean taking it, a chained user bean)
        simply builds it through the factory, so the deferral is invisible to dependants.
        """
        bean_name = getattr(method, "__pyfly_bean_name__", "") or attr_name
        bean_scope = getattr(method, "__pyfly_bean_scope__", Scope.SINGLETON)
        factory = self._bean_factory(config_instance, method)
        declared = self._declared_bean_type(return_type)
        if declared is not None and declared not in self._container._registrations:
            self._container.register(declared, scope=bean_scope, name=bean_name)
            provisional = self._container._registrations[declared]
            provisional.factory = factory
            provisional.destroy_method = self._declared_destroy_method(method)
            if getattr(method, "__pyfly_bean_primary__", False):
                provisional.primary = True
        self._deferred_bean_methods.append(
            _DeferredBeanMethod(config_instance, attr_name, method, return_type, factory, cause)
        )
        logger.debug(
            "bean_method_deferred",
            extra={
                "config": type(config_instance).__qualname__,
                "method": attr_name,
                "waiting_for": getattr(cause.bean_type, "__name__", repr(cause.bean_type)),
            },
        )

    def _process_deferred_bean_methods(self) -> None:
        """Step 2d: complete the user @bean methods deferred in step 2.

        Runs after the auto-configurations, so every bean the framework provides is registered.
        Deferred methods may depend on one another; the pass repeats while it makes progress and
        raises the FIRST unresolved method's own ``NoSuchBeanError`` when it stops — the error
        names the configuration, the method and the parameter exactly as an eager failure did.
        """
        pending = self._deferred_bean_methods
        self._deferred_bean_methods = []
        while pending:
            still_pending: list[_DeferredBeanMethod] = []
            for entry in pending:
                declared = self._declared_bean_type(entry.return_type)
                provisional = self._container._registrations.get(declared) if declared is not None else None
                if (
                    provisional is not None
                    and provisional.factory is entry.factory
                    and provisional.instance is not None
                ):
                    # Somebody resolved the provisional registration in the meantime (an
                    # auto-configured bean or a sibling factory took it); the object exists and
                    # the factory must not run a second time.
                    result = provisional.instance
                else:
                    try:
                        kwargs = self._bean_method_kwargs(entry.config_instance, entry.method)
                    except (NoSuchBeanError, NoUniqueBeanError) as exc:
                        still_pending.append(entry.with_cause(exc))
                        continue
                    result = entry.method(**kwargs)
                self._register_bean_result(
                    entry.config_instance,
                    entry.attr_name,
                    entry.method,
                    entry.return_type,
                    result,
                    factory=entry.factory,
                )
            if len(still_pending) == len(pending):
                first = still_pending[0]
                first_declared = self._declared_bean_type(first.return_type)
                if first_declared is not None:
                    reg = self._container._registrations.get(first_declared)
                    if reg is not None and reg.factory is first.factory and reg.instance is None:
                        # Leave no provisional registration behind that would answer a resolve
                        # with the same failure later.
                        self._remove_registration(first_declared)
                raise first.cause
            pending = still_pending

    @staticmethod
    def _declared_destroy_method(method: Any) -> str:
        """The ``destroy_method`` a ``@bean`` method declares (inferred when it declares none)."""
        from pyfly.container.bean import INFER_DESTROY_METHOD

        return str(getattr(method, "__pyfly_bean_destroy_method__", INFER_DESTROY_METHOD))

    def _bean_factory(self, config_instance: Any, method: Any) -> Callable[[], Any]:
        """Return a zero-arg closure that invokes a @bean method with injection."""
        return lambda: self._call_bean_method(config_instance, method)

    def _call_bean_method(self, config_instance: Any, method: Any) -> Any:
        """Call a @bean method, injecting its parameters from the container."""
        return method(**self._bean_method_kwargs(config_instance, method))

    def _bean_method_kwargs(self, config_instance: Any, method: Any) -> dict[str, Any]:
        """Resolve a @bean method's parameters from the container without calling it.

        Kept apart from the call so a factory whose dependencies are not registered yet can be
        deferred (see :meth:`_process_configurations`) without its body ever having started.

        A parameter that several beans match, none of them ``@primary``, raises
        :class:`NoUniqueBeanError` naming the candidates; it used to be reported as a missing bean.
        """
        hints = typing.get_type_hints(method)
        hints.pop("return", None)
        sig = inspect.signature(method)

        kwargs: dict[str, Any] = {}
        for param_name, param_type in hints.items():
            param = sig.parameters.get(param_name)
            has_default = param is not None and param.default is not inspect.Parameter.empty
            try:
                kwargs[param_name] = self._container._resolve_param(param_type)
            except NoUniqueBeanError as exc:
                if has_default:
                    continue
                raise NoUniqueBeanError(
                    bean_type=exc.bean_type,
                    candidates=exc.candidates,
                    candidate_names=exc.candidate_names,
                    required_by=f"{type(config_instance).__qualname__}.{method.__name__}()",
                    parameter=f"{param_name}: {getattr(param_type, '__name__', repr(param_type))}",
                ) from None
            except NoSuchBeanError:
                if has_default:
                    continue
                raise NoSuchBeanError(
                    bean_type=param_type if isinstance(param_type, type) else None,
                    required_by=f"{type(config_instance).__qualname__}.{method.__name__}()",
                    parameter=f"{param_name}: {getattr(param_type, '__name__', repr(param_type))}",
                ) from None

        return kwargs

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

    def _bind_config_properties(self) -> None:
        """Bind @config_properties beans from config so they inject by type.

        Each registered class carrying ``__pyfly_config_prefix__`` gets a factory
        that produces an instance bound from the active Config (audit #118).
        """
        count = 0
        for cls, reg in self._container._registrations.items():
            if not hasattr(cls, "__pyfly_config_prefix__") or reg.instance is not None or reg.factory is not None:
                continue
            reg.factory = lambda c=cls: self._config.bind(c)
            count += 1
        self._wiring_counts["config_properties"] = count
        if count:
            logger.debug("Bound %d @config_properties bean(s)", count)

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

    def _unique_live_instances(self) -> list[Registration]:
        """Registrations with a resolved instance, de-duplicated by identity.

        An interface-typed @bean is registered under two keys sharing one
        instance; wiring passes must visit each instance once (audit #113). Two beans of one
        class share a by-type slot, so every registration is visited, not only the slots.
        """
        seen: set[int] = set()
        out: list[Registration] = []
        for reg in self._all_registrations():
            if reg.instance is None or id(reg.instance) in seen:
                continue
            seen.add(id(reg.instance))
            out.append(reg)
        return out

    def _wire_app_event_listeners(self) -> None:
        """Scan singleton beans for @app_event_listener methods and subscribe to event bus."""
        count = 0
        for reg in self._unique_live_instances():
            for _attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_app_event_listener__", False):
                    continue
                # Infer event type from the method's parameter hints only — the
                # return annotation must not be mistaken for the event (audit #119).
                hints = typing.get_type_hints(method)
                hints.pop("return", None)
                # The first type-annotated parameter is the event type — any type, so a
                # listener can subscribe to arbitrary domain events, not only ApplicationEvent.
                event_type: type = ApplicationEvent
                for param_type in hints.values():
                    # A concrete class (not typing.Any, which is a class in 3.11+ but is
                    # the "untyped" catch-all here) becomes the subscribed event type.
                    if isinstance(param_type, type) and param_type is not typing.Any:
                        event_type = param_type
                        break
                self._event_bus.subscribe(event_type, method, owner_cls=type(reg.instance), owner=reg.instance)
                self._wired_listener_owners.append(reg.instance)
                count += 1
        self._wiring_counts["event_listeners"] = count
        if count:
            logger.debug("Wired %d @app_event_listener method(s)", count)

    def _wire_message_listeners(self) -> None:
        """Scan beans for @message_listener methods and register with MessageBrokerPort."""
        count = 0
        broker: Any | None = None
        for reg in self._unique_live_instances():
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
                # Apply retry + dead-letter handling (adapter-agnostic) when configured.
                from pyfly.messaging.error_handling import wrap_listener

                handler = wrap_listener(
                    method,
                    broker,
                    retries=getattr(method, "__pyfly_listener_retries__", 0),
                    retry_delay=getattr(method, "__pyfly_listener_retry_delay__", 0.0),
                    dead_letter_topic=getattr(method, "__pyfly_listener_dlq__", None),
                )
                # MessageBrokerPort.subscribe is async; defer via create_task
                task = asyncio.get_running_loop().create_task(broker.subscribe(topic, handler, group=group))
                self._background_tasks.append(task)
                count += 1
        self._wiring_counts["message_listeners"] = count
        if count:
            logger.debug("Wired %d @message_listener method(s)", count)

    def _wire_event_listeners(self) -> None:
        """Scan beans for @event_listener methods and subscribe to the EventPublisher.

        Mirrors @message_listener discovery so context-driven @event_listener
        beans are auto-subscribed without a hand-wired bus (audit #134).
        """
        count = 0
        publisher: Any | None = None
        for reg in self._unique_live_instances():
            for _attr_name, method in self._safe_members(reg.instance):
                if not getattr(method, "__pyfly_event_listener__", False):
                    continue
                if publisher is None:
                    try:
                        from pyfly.eda.ports.outbound import EventPublisher

                        publisher = self._container.resolve(EventPublisher)  # type: ignore[type-abstract]
                    except BeanCreationException:
                        logger.debug("No EventPublisher registered; skipping @event_listener wiring")
                        self._wiring_counts["event_listeners_eda"] = 0
                        return
                for pattern in getattr(method, "__pyfly_event_patterns__", ()):
                    publisher.subscribe(pattern, method)
                    count += 1
        self._wiring_counts["event_listeners_eda"] = count
        if count:
            logger.debug("Wired %d @event_listener subscription(s)", count)

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

        beans = [reg.instance for reg in self._unique_live_instances()]
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
        # Each bean once: a @bean declared as a port is registered under two keys, and scanning both
        # scheduled its methods twice.
        beans = [reg.instance for reg in self._unique_live_instances()]

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

    def _on_bean_created(self, instance: Any, reg: Registration) -> Any:
        """The container's post-create hook: every instance it creates, of every scope, lands here.

        Before the batched passes of step 5 a non-singleton instance is only recorded (they process
        it with the eager singletons); from step 5 on each instance runs the whole pipeline at once.
        """
        if self._startup_created is not None:
            if reg.scope != Scope.SINGLETON:
                self._startup_created.append((instance, reg))
            else:
                self._note_created(instance)
            return instance
        instance = self._post_init_lazy_bean(instance, reg)
        if reg.scope == Scope.SINGLETON:
            self._note_created(instance)
            if self._lifecycle_started and self._has_lifecycle_methods(instance):
                # A @lazy singleton first resolved after start(): the context cannot await its start()
                # in a synchronous resolution, so it neither starts nor stops it. Say so.
                logger.warning(
                    "lifecycle_bean_created_after_start",
                    extra={
                        "bean": reg.display_name,
                        "hint": "the context neither starts nor stops it; make it eager (drop @lazy)",
                    },
                )
        return instance

    def _post_init_lazy_bean(self, instance: Any, reg: Registration) -> Any:
        """Run the full init pipeline on a lazily-created singleton (post-startup):
        BeanPostProcessors (incl. AOP weaving) then @post_construct, mirroring the
        batched startup passes for a single bean. Aspects are already collected
        during startup, so a single bean weaves correctly here.
        """
        bean_name = reg.display_name
        sorted_pps = sorted(self._post_processors, key=lambda pp: get_order(type(pp)))
        applied = self._post_processors_for(sorted_pps, reg.scope)
        for pp in applied:
            instance = pp.before_init(instance, bean_name)
        self._call_post_construct_sync(instance)
        for pp in applied:
            instance = pp.after_init(instance, bean_name)
        self._report_skipped_post_processors(sorted_pps, instance, bean_name, reg.scope)
        return instance

    @staticmethod
    def _singletons_only(post_processor: BeanPostProcessor) -> bool:
        """Whether *post_processor* declares ``singletons_only = True`` (see :class:`BeanPostProcessor`)."""
        return getattr(post_processor, "singletons_only", False) is True

    @classmethod
    def _post_processors_for(cls, sorted_pps: list[BeanPostProcessor], scope: ScopeSpec) -> list[BeanPostProcessor]:
        """The post-processors that process an instance of *scope*: every one for a singleton; for any
        other scope, the ones that do not take singletons only.

        A post-processor that hands the beans it processes to something that outlives them (the
        datasource registry's SPI registrar) would otherwise register a TRANSIENT bean at each
        resolution and keep a REQUEST or refresh-scoped one after its scope ended (Spring's
        ``ApplicationListenerDetector`` registers singletons only for the same reason).
        """
        if scope == Scope.SINGLETON:
            return sorted_pps
        return [pp for pp in sorted_pps if not cls._singletons_only(pp)]

    @classmethod
    def _report_skipped_post_processors(
        cls, sorted_pps: list[BeanPostProcessor], instance: Any, bean_name: str, scope: ScopeSpec
    ) -> None:
        """Tell each singletons-only post-processor that skipped *instance* (its optional
        ``non_singleton_skipped(bean, bean_name, scope)``), so it can say why it ignores the bean."""
        if scope == Scope.SINGLETON:
            return
        for pp in sorted_pps:
            notify = getattr(pp, "non_singleton_skipped", None)
            if cls._singletons_only(pp) and callable(notify):
                notify(instance, bean_name, scope)

    def _call_post_construct_sync(self, instance: Any) -> None:
        """Synchronous @post_construct for lazily-created beans. Async @post_construct
        cannot be awaited in the sync resolution path, so it is skipped with a warning, once per
        class and method (use an eager bean if you need an async @post_construct)."""
        for attr_name, method in _marked_members(instance, "__pyfly_post_construct__"):
            if inspect.iscoroutinefunction(method):
                self._warn_async_post_construct_skipped(instance, attr_name)
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    self._warn_async_post_construct_skipped(instance, attr_name)
            except Exception as exc:
                raise BeanCreationException(
                    subsystem="lifecycle",
                    provider=type(instance).__qualname__,
                    reason=f"@post_construct method '{attr_name}' failed: {exc}",
                ) from exc

    def _warn_async_post_construct_skipped(self, instance: Any, attr_name: str) -> None:
        """Warn that an async ``@post_construct`` was skipped: once per class and method, since a
        transient or request-scoped bean is created again and again."""
        key = (type(instance), attr_name)
        if key in self._skipped_async_post_construct:
            return
        self._skipped_async_post_construct.add(key)
        logger.warning(
            "async_post_construct_skipped_on_lazy_bean",
            extra={"bean": type(instance).__qualname__, "method": attr_name},
        )

    async def _call_post_construct(self, instance: Any) -> None:
        """Call all @post_construct methods on an instance."""
        for attr_name, method in _marked_members(instance, "__pyfly_post_construct__"):
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
        for attr_name, method in _marked_members(instance, "__pyfly_pre_destroy__"):
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
