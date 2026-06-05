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
from typing import Annotated, Any, TypeVar, Union, cast, get_args, get_origin

from pyfly.container.autowired import Autowired
from pyfly.container.bean import Qualifier
from pyfly.container.exceptions import (
    BeanCurrentlyInCreationError,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.metrics import BeanMetrics
from pyfly.container.registry import Registration
from pyfly.container.types import Scope

T = TypeVar("T")


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
        self._resolving: dict[type, None] = {}  # insertion-ordered, O(1) lookup
        self._metrics: dict[type, BeanMetrics] = {}
        # Every registration keyed by (impl_type, name), preserving insertion
        # order. ``_registrations`` keeps only the last registration per type, so
        # this is the source of truth for ``resolve_all``/``list[T]`` — without it,
        # two @bean methods returning the same concrete type would collapse and
        # one bean would silently vanish from type/list resolution.
        self._all: dict[tuple[type, str], Registration] = {}
        self._lock = threading.RLock()

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

        # Multiple impls: pick @primary
        for impl in impls:
            if getattr(impl, "__pyfly_primary__", False):
                return cast(T, self._resolve_registration(self._registrations[impl]))

        raise NoUniqueBeanError(bean_type=cls, candidates=impls)

    def resolve_by_name(self, name: str) -> Any:
        """Resolve a bean by its registered name."""
        if name not in self._named:
            raise NoSuchBeanError(
                bean_name=name,
                suggestions=list(self._named.keys()),
            )
        return self._resolve_registration(self._named[name])

    def resolve_all(self, cls: type[T]) -> list[T]:
        """Resolve every bean assignable to *cls*.

        Includes both implementations bound to an interface AND beans whose own
        concrete type is *cls* (so multiple @bean methods returning the same
        concrete type are all returned). Deduplicated by resolved-instance
        identity so the synthetic interface registration does not double-count
        an already-bound implementation.
        """
        results: list[T] = []
        seen: set[int] = set()

        def _add(reg: Registration) -> None:
            instance = self._resolve_registration(reg)
            if id(instance) not in seen:
                seen.add(id(instance))
                results.append(cast(T, instance))

        for impl in self._bindings.get(cls, []):
            reg = self._registrations.get(impl)
            if reg is not None:
                _add(reg)
        for reg in self._all.values():
            if reg.impl_type is cls:
                _add(reg)
        return results

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
        # Handle Annotated[T, Qualifier("name")]
        if get_origin(param_type) is Annotated:
            args = get_args(param_type)
            base_type = args[0]
            for metadata in args[1:]:
                if isinstance(metadata, Qualifier):
                    return self.resolve_by_name(metadata.name)
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

        # Handle type[T] or bare `type` — class references cannot be auto-resolved
        if param_type is type or get_origin(param_type) is type:
            raise NoSuchBeanError(
                bean_type=param_type if isinstance(param_type, type) else None,
            )

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
                value = self.resolve_by_name(default.qualifier)
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
