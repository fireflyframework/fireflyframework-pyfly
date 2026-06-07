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
"""Lightweight DI container with type-hint based resolution."""

from __future__ import annotations

import difflib
import inspect
import logging
import threading
import time
import types
import typing
from collections.abc import Callable
from typing import Annotated, Any, TypeVar, Union, cast, get_args, get_origin

from pyfly.container.autowired import Autowired
from pyfly.container.bean import Qualifier
from pyfly.container.exceptions import (
    BeanCurrentlyInCreationError,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.metrics import BeanMetrics
from pyfly.container.ordering import get_order
from pyfly.container.provider import Provider
from pyfly.container.registry import Registration
from pyfly.container.types import Scope

T = TypeVar("T")


def _assignable(instance: Any, expected_type: Any) -> bool:
    """Best-effort ``isinstance`` that tolerates non-runtime-checkable Protocols
    and subscripted generics (which raise on ``isinstance``) by accepting them.

    Used to verify a ``Qualifier``-named bean is of the declared type, so a
    mistyped qualifier raises instead of silently injecting an incompatible bean.
    """
    try:
        return isinstance(instance, expected_type)
    except TypeError:
        return True


def _coerce_value(resolved: Any, base_type: Any) -> Any:
    """Best-effort coercion of a resolved @Value to the declared parameter type."""
    if not isinstance(base_type, type) or isinstance(resolved, base_type):
        return resolved
    if base_type is bool:
        return str(resolved).strip().lower() in ("true", "1", "yes", "on")
    if base_type in (int, float, str):
        try:
            return base_type(resolved)
        except (TypeError, ValueError):
            return resolved
    return resolved


def _safe_issubclass(impl: Any, origin: Any) -> bool:
    """``issubclass`` that returns ``False`` instead of raising for non-class args."""
    try:
        return isinstance(impl, type) and issubclass(impl, origin)
    except TypeError:
        return False


def _collect_generic_args(cls: Any) -> set[type]:
    """Concrete (non-TypeVar) type arguments from a class's generic bases, recursively.

    e.g. ``class UserRepository(Repository[User, UUID])`` -> ``{User, UUID}``.
    """
    found: set[type] = set()
    for base in getattr(cls, "__orig_bases__", ()):
        for arg in get_args(base):
            if isinstance(arg, type):
                found.add(arg)
        base_origin = get_origin(base)
        if base_origin is not None and base_origin is not cls and hasattr(base_origin, "__orig_bases__"):
            found |= _collect_generic_args(base_origin)
    return found


class Container:
    """Dependency injection container.

    Supports constructor injection via type hints, field injection via
    ``Autowired``, scoped lifecycles, interface-to-implementation binding,
    named beans, @primary resolution, Qualifier-based disambiguation,
    ``Optional[T]`` and ``list[T]`` parameter types, and circular dependency
    detection.
    """

    def __init__(self) -> None:
        self._registrations: dict[type, Registration] = {}
        self._named: dict[str, Registration] = {}
        self._bindings: dict[type, list[type]] = {}
        # Per-thread in-creation set for cycle detection — thread-local so
        # concurrent TRANSIENT/REQUEST resolution (which does not hold the lock)
        # can't race on a shared dict or raise spurious circular-dependency errors.
        self._resolving_local = threading.local()
        self._metrics: dict[type, BeanMetrics] = {}
        # Every registration keyed by (impl_type, name), preserving insertion
        # order. ``_registrations`` keeps only the last registration per type, so
        # this is the source of truth for ``resolve_all``/``list[T]`` — without it,
        # two @bean methods returning the same concrete type would collapse and
        # one bean would silently vanish from type/list resolution.
        self._all: dict[tuple[type, str], Registration] = {}
        self._lock = threading.RLock()
        # Installed by ApplicationContext AFTER startup so SINGLETON beans created
        # lazily (post-startup) still run the full init pipeline (BeanPostProcessors,
        # @post_construct, AOP weaving). None during startup — the batched startup
        # passes handle eager beans then (avoids double-initialization).
        self._post_create_hook: Callable[[Any, Registration], Any] | None = None

    @property
    def _resolving(self) -> dict[type, None]:
        """This thread's in-creation set (insertion-ordered for cycle chains)."""
        stack: dict[type, None] | None = getattr(self._resolving_local, "stack", None)
        if stack is None:
            stack = {}
            self._resolving_local.stack = stack
        return stack

    def register(
        self,
        cls: type,
        scope: Scope = Scope.SINGLETON,
        condition: Any = None,
        name: str = "",
    ) -> None:
        """Register a class for injection."""
        bean_name = name or getattr(cls, "__pyfly_bean_name__", "")
        bean_scope = getattr(cls, "__pyfly_scope__", None) or scope
        reg = Registration(
            impl_type=cls,
            scope=bean_scope,
            condition=condition,
            name=bean_name,
        )
        self._registrations[cls] = reg
        self._all[(cls, bean_name)] = reg
        if bean_name:
            self._named[bean_name] = reg

    def bind(self, interface: type, implementation: type) -> None:
        """Bind an interface/base class to a concrete implementation."""
        if interface not in self._bindings:
            self._bindings[interface] = []
        if implementation not in self._bindings[interface]:
            self._bindings[interface].append(implementation)

    def resolve(self, cls: type[T]) -> T:
        """Resolve an instance of the given type."""
        # Direct registration
        if cls in self._registrations:
            return cast(T, self._resolve_registration(self._registrations[cls]))

        # Follow binding(s)
        impls = self._bindings.get(cls, [])
        if not impls:
            raise NoSuchBeanError(
                bean_type=cls,
                suggestions=self._get_similar_type_names(
                    getattr(cls, "__name__", ""),
                ),
            )

        if len(impls) == 1:
            return cast(T, self._resolve_registration(self._registrations[impls[0]]))

        # Multiple impls: pick @primary — a class-level marker OR an @bean-level
        # primary recorded on the registration (the @Bean @Primary equivalent).
        for impl in impls:
            reg = self._registrations.get(impl)
            if getattr(impl, "__pyfly_primary__", False) or (reg is not None and reg.primary):
                return cast(T, self._resolve_registration(self._registrations[impl]))

        raise NoUniqueBeanError(bean_type=cls, candidates=impls)

    def resolve_by_name(self, name: str, expected_type: type | None = None) -> Any:
        """Resolve a bean by its registered name.

        When *expected_type* is given, the named bean must be assignable to it —
        otherwise a mistyped ``Qualifier`` would silently inject an incompatible
        object. Protocols/generics that cannot be ``isinstance``-checked are accepted.
        """
        if name not in self._named:
            raise NoSuchBeanError(
                bean_name=name,
                suggestions=list(self._named.keys()),
            )
        instance = self._resolve_registration(self._named[name])
        if expected_type is not None and not _assignable(instance, expected_type):
            raise NoSuchBeanError(
                bean_name=name,
                bean_type=expected_type if isinstance(expected_type, type) else None,
                suggestions=[
                    f"bean {name!r} is a {type(instance).__name__}, not assignable to "
                    f"{getattr(expected_type, '__name__', expected_type)!r}"
                ],
            )
        return instance

    def resolve_all(self, cls: type[T]) -> list[T]:
        """Resolve every bean assignable to *cls*.

        Includes both implementations bound to an interface AND beans whose own
        concrete type is *cls* (so multiple @bean methods returning the same
        concrete type are all returned). Deduplicated by resolved-instance
        identity so the synthetic interface registration does not double-count
        an already-bound implementation.
        """
        ordered: list[tuple[int, Any]] = []
        seen: set[int] = set()

        def _add(reg: Registration) -> None:
            instance = self._resolve_registration(reg)
            if id(instance) not in seen:
                seen.add(id(instance))
                ordered.append((get_order(reg.impl_type), instance))

        for impl in self._bindings.get(cls, []):
            reg = self._registrations.get(impl)
            if reg is not None:
                _add(reg)
        for reg in self._all.values():
            if reg.impl_type is cls:
                _add(reg)
        # Honor @order for injected list[T] (Spring orders List<T> by @Order);
        # stable sort keeps registration order within the same @order value.
        ordered.sort(key=lambda pair: pair[0])
        return [cast(T, instance) for _, instance in ordered]

    def _resolve_map(self, value_type: Any) -> dict[str, Any]:
        """Map injection: ``{bean-name: bean}`` for every named bean assignable to *value_type*."""
        result: dict[str, Any] = {}
        for name, reg in self._named.items():
            instance = self._resolve_registration(reg)
            if _assignable(instance, value_type):
                result[name] = instance
        return result

    def _resolve_generic(self, origin: type, type_args: tuple[Any, ...]) -> Any | None:
        """Resolve a parametrized generic, e.g. ``Repository[User]`` (Spring generic-aware injection).

        Matches a registered subclass of *origin* whose generic bases carry all the
        requested concrete type args. Returns ``None`` when *origin* has no
        registered subclasses (so the caller resolves *origin* normally); raises
        ``NoSuchBeanError`` when the family exists but nothing matches the args.
        """
        wanted = [a for a in type_args if isinstance(a, type)]
        family = [impl for impl in self._registrations if impl is not origin and _safe_issubclass(impl, origin)]
        if not family:
            return None
        matches = [impl for impl in family if wanted and all(w in _collect_generic_args(impl) for w in wanted)]
        if len(matches) == 1:
            return self._resolve_registration(self._registrations[matches[0]])
        if len(matches) > 1:
            for impl in matches:
                reg = self._registrations.get(impl)
                if getattr(impl, "__pyfly_primary__", False) or (reg is not None and reg.primary):
                    return self._resolve_registration(self._registrations[impl])
            raise NoUniqueBeanError(bean_type=origin, candidates=matches)
        raise NoSuchBeanError(
            bean_type=origin,
            suggestions=[
                f"no {getattr(origin, '__name__', origin)} implementation parametrized with "
                f"{[getattr(w, '__name__', w) for w in wanted]}"
            ],
        )

    def _get_config(self) -> Any:
        """The Config bean instance — required to resolve @Value placeholders."""
        from pyfly.core.config import Config

        reg = self._registrations.get(Config)
        if reg is None or reg.instance is None:
            raise NoSuchBeanError(
                bean_type=Config,
                suggestions=["@Value requires the Config bean to be registered"],
            )
        return reg.instance

    def contains(self, name: str) -> bool:
        """Check if a named bean exists."""
        return name in self._named

    def _resolve_registration(self, reg: Registration) -> Any:
        """Resolve a single registration, handling scope."""
        if reg.scope == Scope.SINGLETON:
            if reg.instance is not None:
                self._ensure_metrics(reg.impl_type).resolution_count += 1
                return reg.instance
            with self._lock:
                # Double-check after acquiring lock
                if reg.instance is not None:
                    self._ensure_metrics(reg.impl_type).resolution_count += 1
                    return reg.instance
                instance = self._create_instance(reg)
                if self._post_create_hook is not None:
                    # Lazily-created singleton (post-startup): run the full init pipeline.
                    instance = self._post_create_hook(instance, reg)
                reg.instance = instance
                self._ensure_metrics(reg.impl_type).resolution_count += 1
                return instance

        if reg.scope == Scope.REQUEST:
            instance = self._resolve_request_scoped(reg)
            self._ensure_metrics(reg.impl_type).resolution_count += 1
            return instance

        instance = self._create_instance(reg)
        self._ensure_metrics(reg.impl_type).resolution_count += 1
        return instance

    def _resolve_request_scoped(self, reg: Registration) -> Any:
        """Resolve a REQUEST-scoped bean from the active RequestContext."""
        from pyfly.context.request_context import RequestContext

        ctx = RequestContext.current()
        if ctx is None:
            raise RuntimeError(
                f"No active request context for REQUEST-scoped bean "
                f"{reg.impl_type.__name__}. Ensure a RequestContextFilter is active."
            )

        # Store request-scoped instances in the context's attributes
        cache_key = f"__pyfly_bean_{reg.impl_type.__qualname__}"
        existing = ctx.get(cache_key)
        if existing is not None:
            return existing

        instance = self._create_instance(reg)
        ctx.set(cache_key, instance)
        return instance

    def _create_instance(self, reg: Registration) -> Any:
        """Create an instance, resolving constructor and field dependencies."""
        if reg.impl_type in self._resolving:
            chain = list(self._resolving.keys())
            raise BeanCurrentlyInCreationError(chain=chain, current=reg.impl_type)
        self._resolving[reg.impl_type] = None
        try:
            start = time.perf_counter_ns()

            # A registration backed by a factory (e.g. a @bean method) must be
            # built through that factory so its construction logic is preserved
            # on every resolution (notably TRANSIENT @bean beans).
            if reg.factory is not None:
                instance = reg.factory()
                self._inject_autowired_fields(instance)
                metrics = self._ensure_metrics(reg.impl_type)
                metrics.creation_time_ns = time.perf_counter_ns() - start
                metrics.created_at = time.time()
                return instance

            init = reg.impl_type.__init__  # type: ignore[misc]
            if init is object.__init__:
                instance = reg.impl_type()
            else:
                hints = typing.get_type_hints(init, include_extras=True)
                hints.pop("return", None)
                sig = inspect.signature(init)

                kwargs: dict[str, Any] = {}
                for param_name, param_type in hints.items():
                    param = sig.parameters.get(param_name)
                    has_default = param is not None and param.default is not inspect.Parameter.empty
                    try:
                        kwargs[param_name] = self._resolve_param(param_type)
                    except (NoSuchBeanError, NoUniqueBeanError):
                        if has_default:
                            continue
                        raise NoSuchBeanError(
                            bean_type=param_type if isinstance(param_type, type) else None,
                            required_by=f"{reg.impl_type.__qualname__}.__init__()",
                            parameter=f"{param_name}: {getattr(param_type, '__name__', repr(param_type))}",
                            suggestions=self._get_similar_type_names(
                                getattr(param_type, "__name__", ""),
                            ),
                        ) from None

                instance = reg.impl_type(**kwargs)

            self._inject_autowired_fields(instance)

            elapsed = time.perf_counter_ns() - start
            metrics = self._ensure_metrics(reg.impl_type)
            metrics.creation_time_ns = elapsed
            metrics.created_at = time.time()

            return instance
        finally:
            self._resolving.pop(reg.impl_type, None)

    def _resolve_param(self, param_type: type) -> Any:
        """Resolve a single parameter, handling Annotated, Optional, and list."""
        # Handle Annotated[T, Qualifier("name")] and Annotated[T, Value("${key}")]
        if get_origin(param_type) is Annotated:
            from pyfly.core.value import Value

            args = get_args(param_type)
            base_type = args[0]
            for metadata in args[1:]:
                if isinstance(metadata, Qualifier):
                    return self.resolve_by_name(metadata.name, expected_type=base_type)
                if isinstance(metadata, Value):
                    return _coerce_value(metadata.resolve(self._get_config()), base_type)
            return self._resolve_param(base_type)

        # Handle Optional[T] (Union[T, None] or T | None via PEP 604)
        if get_origin(param_type) is Union or isinstance(param_type, types.UnionType):
            args = get_args(param_type)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                try:
                    return self.resolve(non_none[0])
                except (NoSuchBeanError, NoUniqueBeanError):
                    return None

        # Handle list[T]
        if get_origin(param_type) is list:
            args = get_args(param_type)
            if args:
                return self.resolve_all(args[0])

        # Handle Provider[T] — deferred / fresh resolution (Spring ObjectFactory)
        if get_origin(param_type) is Provider:
            args = get_args(param_type)
            if args:
                return Provider(self, args[0])

        # Handle dict[str, T] — Map injection (bean-name -> bean), like Spring Map<String,T>
        if get_origin(param_type) is dict:
            args = get_args(param_type)
            if len(args) == 2 and args[0] is str:
                return self._resolve_map(args[1])

        # Handle type[T] or bare `type` — class references cannot be auto-resolved
        if param_type is type or get_origin(param_type) is type:
            raise NoSuchBeanError(
                bean_type=param_type if isinstance(param_type, type) else None,
            )

        # Handle a parametrized generic interface, e.g. Repository[User] -> an impl
        # parametrized with User (Spring's generic-aware injection). Falls back to
        # resolving the bare origin when there is no generic family to match against.
        origin = get_origin(param_type)
        if isinstance(origin, type) and origin not in (list, set, frozenset, tuple, dict):
            resolved = self._resolve_generic(origin, get_args(param_type))
            if resolved is not None:
                return resolved
            return self.resolve(origin)

        return self.resolve(param_type)

    def _inject_autowired_fields(self, instance: Any) -> None:
        """Inject dependencies into fields marked with Autowired() or Value()."""
        from pyfly.core.value import Value

        try:
            hints = typing.get_type_hints(type(instance), include_extras=True)
        except NameError:
            logging.getLogger(__name__).warning(
                "Could not resolve type hints for %s — Autowired fields will not be injected. "
                "Check for unresolved forward references.",
                type(instance).__qualname__,
            )
            return

        for attr_name, attr_type in hints.items():
            default = getattr(type(instance), attr_name, None)

            # Handle @Value("${key}") field descriptors
            if isinstance(default, Value):
                from pyfly.core.config import Config

                config_reg = self._registrations.get(Config)
                if config_reg is None or config_reg.instance is None:
                    raise RuntimeError(
                        f"Cannot resolve @Value for {type(instance).__qualname__}.{attr_name}: "
                        f"Config bean not registered"
                    )
                resolved = default.resolve(config_reg.instance)
                setattr(instance, attr_name, resolved)
                continue

            if not isinstance(default, Autowired):
                continue

            if default.qualifier:
                base = get_args(attr_type)[0] if get_origin(attr_type) is Annotated else attr_type
                value = self.resolve_by_name(default.qualifier, expected_type=base)
            elif get_origin(attr_type) is Annotated:
                value = self._resolve_param(attr_type)
            else:
                try:
                    value = self.resolve(attr_type)
                except (NoSuchBeanError, NoUniqueBeanError):
                    if not default.required:
                        value = None
                    else:
                        raise NoSuchBeanError(
                            bean_type=attr_type if isinstance(attr_type, type) else None,
                            required_by=f"{type(instance).__qualname__}.{attr_name}",
                            parameter=f"{attr_name}: {getattr(attr_type, '__name__', repr(attr_type))} = Autowired()",
                        ) from None

            setattr(instance, attr_name, value)

    def _ensure_metrics(self, cls: type) -> BeanMetrics:
        """Return the metrics for *cls*, creating a new entry if needed."""
        if cls not in self._metrics:
            self._metrics[cls] = BeanMetrics()
        return self._metrics[cls]

    def get_bean_metrics(self, cls: type) -> BeanMetrics | None:
        """Return collected metrics for a single bean, or ``None`` if never resolved."""
        return self._metrics.get(cls)

    def get_all_metrics(self) -> dict[type, BeanMetrics]:
        """Return a snapshot of metrics for every resolved bean."""
        return dict(self._metrics)

    def _get_similar_type_names(self, name: str) -> list[str]:
        """Return registered type names similar to *name* using fuzzy matching."""
        if not name:
            return []
        registered_names = [getattr(cls, "__name__", repr(cls)) for cls in self._registrations]
        return difflib.get_close_matches(name, registered_names, n=5, cutoff=0.4)
