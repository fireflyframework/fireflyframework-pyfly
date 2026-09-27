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

import contextlib
import difflib
import inspect
import logging
import threading
import time
import types
import typing
import weakref
from collections.abc import Callable
from typing import Annotated, Any, TypeVar, Union, cast, get_args, get_origin

from pyfly.container.autowired import Autowired
from pyfly.container.bean import Qualifier
from pyfly.container.exceptions import (
    BeanCreationException,
    BeanCreationNotAllowedError,
    BeanCurrentlyInCreationError,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.metrics import BeanMetrics
from pyfly.container.ordering import get_order
from pyfly.container.provider import Provider
from pyfly.container.registry import Registration
from pyfly.container.types import Scope, ScopeHandler, ScopeSpec

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


# The Autowired/Value fields of each class, found once per class (weakly keyed, so classes defined
# at runtime, in tests for instance, are not kept alive by the cache).
_FIELD_CACHE: weakref.WeakKeyDictionary[type, tuple[tuple[str, type, Any], ...]] = weakref.WeakKeyDictionary()


def _injected_fields(cls: type) -> tuple[tuple[str, type, Any], ...]:
    """``(name, declaring class, descriptor)`` for every annotated ``Autowired``/``Value`` field of *cls*.

    Only the class dictionaries are read, so a class without such fields costs a few dictionary
    lookups and no annotation is evaluated. The declaring class is the first class in the MRO that
    annotates the name; its module is where the annotation is resolved.
    """
    try:
        return _FIELD_CACHE[cls]
    except (KeyError, TypeError):
        pass
    from pyfly.core.value import Value

    fields: list[tuple[str, type, Any]] = []
    seen: set[str] = set()
    for klass in cls.__mro__:
        annotations = klass.__dict__.get("__annotations__", {})
        for name in annotations:
            if name in seen:
                continue
            seen.add(name)
            default = getattr(cls, name, None)
            if isinstance(default, (Autowired, Value)):
                fields.append((name, klass, default))
    result = tuple(fields)
    with contextlib.suppress(TypeError):  # a class that cannot be weakly referenced is not cached
        _FIELD_CACHE[cls] = result
    return result


def _field_hint(owner: type, name: str) -> Any:
    """The resolved annotation of *name*, evaluated alone in the module and namespace of *owner*.

    ``typing.get_type_hints`` on the whole class fails as soon as ANY annotation of ANY class in the
    MRO cannot be resolved; a probe class that carries just this one annotation does not.
    """
    raw = owner.__dict__["__annotations__"][name]
    probe = types.new_class(
        f"{owner.__name__}_{name}_hint",
        exec_body=lambda namespace: namespace.update({"__annotations__": {name: raw}, "__module__": owner.__module__}),
    )
    return typing.get_type_hints(probe, localns=dict(vars(owner)), include_extras=True)[name]


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


def scope_key(reg: Registration) -> str:
    """The key a scope caches the instance of *reg* under: one per bean definition.

    It names the definition, not the class: the module-qualified class and, for a named bean, its
    name. Two ``@bean`` methods that return one class (two request- or refresh-scoped session
    factories, one per database) are two definitions with two keys, and so are two classes that share
    a ``__qualname__`` in different modules. Keying by ``__qualname__`` alone (26.09.07 and earlier)
    handed every name after the first the first one's instance.
    """
    impl = reg.impl_type
    qualified = f"{getattr(impl, '__module__', '')}.{getattr(impl, '__qualname__', reg.display_name)}"
    return f"__pyfly_bean_{qualified}#{reg.name}" if reg.name else f"__pyfly_bean_{qualified}"


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
        # Custom bean scopes registered via register_scope() (Spring's registerScope).
        self._custom_scopes: dict[str, ScopeHandler] = {}
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
        # Called with every instance the container creates, of every scope, before it is cached or
        # handed out; it returns the instance to use. ApplicationContext installs it for the whole
        # start() and after it, so a TRANSIENT, REQUEST, SESSION or custom-scoped bean, and a @lazy
        # singleton first resolved while the context starts, get the BeanPostProcessors and
        # @post_construct like any eager singleton. None for a bare container.
        self._post_create_hook: Callable[[Any, Registration], Any] | None = None
        # The scoped proxy of each proxied registration, by registration identity (the registration
        # is kept with it, so a recycled id never hands out another registration's proxy).
        self._scoped_proxies: dict[int, tuple[Registration, Any]] = {}
        # Why the container builds no bean at the moment, or None while it may. ApplicationContext
        # sets it when stop() starts destroying beans and clears it when start() begins, so a
        # stopped context never rebuilds a singleton it released (an engine nobody would dispose).
        self._creation_refused: str | None = None

    @property
    def _resolving(self) -> dict[type, None]:
        """This thread's in-creation set (insertion-ordered for cycle chains)."""
        stack: dict[type, None] | None = getattr(self._resolving_local, "stack", None)
        if stack is None:
            stack = {}
            self._resolving_local.stack = stack
        return stack

    def refuse_creation(self, reason: str) -> None:
        """Build no bean from now on; resolving one that does not exist yet raises
        :class:`~pyfly.container.exceptions.BeanCreationNotAllowedError` with *reason*.

        The instances that exist are still handed out. This is Spring's "singletons currently in
        destruction" state; :meth:`allow_creation` ends it.
        """
        self._creation_refused = reason

    def allow_creation(self) -> None:
        """Build beans again (see :meth:`refuse_creation`)."""
        self._creation_refused = None

    @property
    def creation_refused(self) -> bool:
        """Whether the container currently refuses to build beans."""
        return self._creation_refused is not None

    def register_scope(self, name: str, handler: ScopeHandler) -> None:
        """Register a custom bean scope (Spring's ``ConfigurableBeanFactory.registerScope``).

        Beans declared with ``scope=name`` are resolved through *handler*. Built-in scope
        names are reserved and cannot be overridden.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Custom scope name must be a non-empty string")
        if name in ("singleton", "transient", "request", "session"):
            raise ValueError(f"Cannot override built-in scope name: {name!r}")
        self._custom_scopes[name] = handler

    def unregister_scope(self, name: str) -> None:
        """Remove a previously registered custom scope (no-op if absent)."""
        self._custom_scopes.pop(name, None)

    def register(
        self,
        cls: type,
        scope: ScopeSpec = Scope.SINGLETON,
        condition: Any = None,
        name: str = "",
    ) -> None:
        """Register a class for injection."""
        from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME

        bean_name = name or getattr(cls, "__pyfly_bean_name__", "")
        bean_scope = getattr(cls, "__pyfly_scope__", None) or scope
        if getattr(cls, "__pyfly_refresh_scope__", False):
            # @refresh_scope survives a stereotype applied after it (which resets __pyfly_scope__).
            bean_scope = REFRESH_SCOPE_NAME
        reg = Registration(
            impl_type=cls,
            scope=bean_scope,
            condition=condition,
            name=bean_name,
            scoped_proxy=bool(getattr(cls, "__pyfly_scoped_proxy__", False)),
        )
        self._registrations[cls] = reg
        self._all[(cls, bean_name)] = reg
        if bean_name:
            self._named[bean_name] = reg

    # ------------------------------------------------------------------
    # Public introspection / registration SPI
    # ------------------------------------------------------------------

    def register_instance(self, cls: type, instance: Any, *, name: str = "") -> None:
        """Register an already-constructed object as a SINGLETON bean.

        The supported way to install a pre-built instance (Spring's
        ``registerSingleton``) — preferred over mutating registration internals.
        """
        self.register(cls, scope=Scope.SINGLETON, name=name)
        self._registrations[cls].instance = instance

    def contains_type(self, cls: type) -> bool:
        """Whether a bean registered under exactly *cls* exists."""
        return cls in self._registrations

    def get_registration(self, cls: type) -> Registration | None:
        """Return the :class:`Registration` for *cls*, or ``None`` if unregistered."""
        return self._registrations.get(cls)

    def registered_types(self) -> list[type]:
        """Snapshot of all registered bean types."""
        return list(self._registrations)

    def reset_instance(self, cls: type) -> Any | None:
        """Drop the cached SINGLETON instance of *cls* so it is rebuilt on next resolve.

        Returns the evicted instance (or ``None``). Used by refresh/config-reload to force
        re-creation without reaching into registration internals.
        """
        reg = self._registrations.get(cls)
        if reg is None:
            return None
        previous, reg.instance = reg.instance, None
        return previous

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
            return cast(T, self._resolve_registration(self._registration_for(cls)))

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
            return cast(T, self._resolve_registration(self._registration_for(impls[0])))

        # Multiple impls: pick @primary — a class-level marker OR an @bean-level
        # primary recorded on the registration (the @Bean @Primary equivalent).
        for impl in impls:
            reg = self._registrations.get(impl)
            if getattr(impl, "__pyfly_primary__", False) or (reg is not None and reg.primary):
                return cast(T, self._resolve_registration(self._registration_for(impl)))

        raise NoUniqueBeanError(bean_type=cls, candidates=impls)

    def _registration_for(self, cls: type) -> Registration:
        """The registration that answers a lookup of exactly *cls*.

        The by-type slot holds the LAST registration of a class, but two named beans can share a
        class (two ``@bean`` methods returning ``AsyncEngine``). The slot used to answer anyway, so the
        bean registered last silently shadowed the others, ``@bean(primary=True)`` included. When
        several distinct beans share *cls*, the ``@primary`` one answers; without exactly one primary
        the lookup is ambiguous and raises :class:`NoUniqueBeanError` (Spring's behavior). Resolve
        one of them by name or ``Qualifier``, or all of them with ``list[T]``.
        """
        slot = self._registrations[cls]
        siblings = self._same_type_registrations(cls)
        if len(siblings) < 2:
            return slot
        primaries = [reg for reg in siblings if reg.primary or getattr(reg.impl_type, "__pyfly_primary__", False)]
        if len(primaries) == 1:
            return primaries[0]
        raise NoUniqueBeanError(
            bean_type=cls,
            candidates=[cls for _ in siblings],
            candidate_names=[reg.display_name for reg in siblings],
        )

    def _same_type_registrations(self, cls: type) -> list[Registration]:
        """The distinct beans registered under exactly *cls* (one per registration, or one per shared
        factory/instance: an ``@bean``'s aliases are one bean)."""
        found: list[Registration] = []
        seen: set[int] = set()
        for (registered, _name), reg in self._all.items():
            if registered is not cls:
                continue
            identity = id(reg.instance) if reg.instance is not None else id(reg.factory) if reg.factory else id(reg)
            if identity in seen:
                continue
            seen.add(identity)
            found.append(reg)
        return found

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
                instance = self._create_initialized(reg)
                reg.instance = instance
                self._ensure_metrics(reg.impl_type).resolution_count += 1
                return instance

        if reg.scoped_proxy and reg.scope != Scope.TRANSIENT:
            return self._scoped_proxy(reg)
        return self._resolve_scoped(reg)

    def _scoped_proxy(self, reg: Registration) -> Any:
        """The one :class:`~pyfly.container.scoped_proxy.ScopedProxy` of *reg* (built on first use)."""
        from pyfly.container.scoped_proxy import ScopedProxy

        proxy = self._scoped_proxies.get(id(reg))
        if proxy is None or proxy[0] is not reg:
            target_type = reg.impl_type if isinstance(reg.impl_type, type) else object
            proxy = (reg, ScopedProxy(lambda: self._resolve_scoped(reg), target_type))
            self._scoped_proxies[id(reg)] = proxy
        return proxy[1]

    def _resolve_scoped(self, reg: Registration) -> Any:
        """The instance a non-singleton registration's scope holds now (created when it holds none)."""
        if reg.scope == Scope.REQUEST:
            instance = self._resolve_request_scoped(reg)
            self._ensure_metrics(reg.impl_type).resolution_count += 1
            return instance

        if reg.scope == Scope.SESSION:
            instance = self._resolve_session_scoped(reg)
            self._ensure_metrics(reg.impl_type).resolution_count += 1
            return instance

        if isinstance(reg.scope, str):
            instance = self._resolve_custom_scoped(reg)
            self._ensure_metrics(reg.impl_type).resolution_count += 1
            return instance

        instance = self._create_initialized(reg)
        self._ensure_metrics(reg.impl_type).resolution_count += 1
        return instance

    def _create_initialized(self, reg: Registration) -> Any:
        """Create an instance of *reg* and run the post-create hook on it (the init pipeline)."""
        if self._creation_refused is not None:
            raise BeanCreationNotAllowedError(bean=reg.display_name, reason=self._creation_refused)
        instance = self._create_instance(reg)
        hook = self._post_create_hook
        return instance if hook is None else hook(instance, reg)

    def _resolve_request_scoped(self, reg: Registration) -> Any:
        """Resolve a REQUEST-scoped bean from the active RequestContext."""
        from pyfly.context.request_context import RequestContext

        ctx = RequestContext.current()
        if ctx is None:
            raise RuntimeError(
                f"No active request context for REQUEST-scoped bean "
                f"{reg.display_name}. Ensure a RequestContextFilter is active."
            )

        # Store request-scoped instances in the context's attributes
        cache_key = scope_key(reg)
        existing = ctx.get(cache_key)
        if existing is not None:
            return existing

        instance = self._create_initialized(reg)
        ctx.set(cache_key, instance)
        return instance

    def _resolve_session_scoped(self, reg: Registration) -> Any:
        """Resolve a SESSION-scoped bean from the active HttpSession's attributes.

        The bean lives as a session attribute, so (like Spring's session-scoped beans) it
        is persisted with the session — it must be serializable when a non-memory session
        store (e.g. Redis) is used.
        """
        from pyfly.context.request_context import HTTP_SESSION_KEY, RequestContext

        ctx = RequestContext.current()
        if ctx is None:
            raise RuntimeError(
                f"No active request context for SESSION-scoped bean "
                f"{reg.display_name}. Ensure a RequestContextFilter is active."
            )
        session = ctx.get(HTTP_SESSION_KEY)
        if session is None:
            raise RuntimeError(
                f"No HTTP session for SESSION-scoped bean {reg.display_name}. "
                f"Ensure the session module (SessionFilter) is enabled."
            )

        cache_key = scope_key(reg)
        existing = session.get_attribute(cache_key)
        if existing is not None:
            return existing
        existing = self._adopt_legacy_session_attribute(session, reg, cache_key)
        if existing is not None:
            return existing

        instance = self._create_initialized(reg)
        session.set_attribute(cache_key, instance)
        return instance

    def _adopt_legacy_session_attribute(self, session: Any, reg: Registration, cache_key: str) -> Any | None:
        """The instance a session stored under the key of 26.09.07 and earlier, moved to *cache_key*.

        Until 26.09.07 a SESSION-scoped bean lived under ``__pyfly_bean_<class qualname>``, and a
        session persisted in a store (Redis) still carries that key. It is adopted only when exactly
        one SESSION-scoped registration maps to it and the stored object is of that registration's
        type; when two beans shared the key, nobody can tell whose object it is, and it is left alone.
        """
        legacy_key = f"__pyfly_bean_{reg.impl_type.__qualname__}"
        if legacy_key == cache_key:
            return None
        stored = session.get_attribute(legacy_key)
        if stored is None or not _assignable(stored, reg.impl_type):
            return None
        sharing = {
            id(candidate)
            for candidate in (*self._registrations.values(), *self._all.values(), *self._named.values())
            if candidate.scope == Scope.SESSION and f"__pyfly_bean_{candidate.impl_type.__qualname__}" == legacy_key
        }
        if len(sharing) != 1:
            return None
        session.set_attribute(cache_key, stored)
        session.remove_attribute(legacy_key)
        return stored

    def _resolve_custom_scoped(self, reg: Registration) -> Any:
        """Resolve a bean through a custom :class:`ScopeHandler` registered by name."""
        handler = self._custom_scopes.get(cast(str, reg.scope))
        if handler is None:
            raise RuntimeError(
                f"Custom scope {reg.scope!r} is not registered for bean "
                f"{reg.display_name}. Available: {sorted(self._custom_scopes)}. "
                f"Call container.register_scope({reg.scope!r}, handler) first."
            )
        return handler.get(scope_key(reg), lambda: self._create_initialized(reg))

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

            # Constructor injection plan is parsed once and cached on the registration —
            # get_type_hints/inspect.signature are NOT re-run per resolve (notably for TRANSIENT).
            if not reg.init_plan_built:
                reg.init_plan = self._build_init_plan(reg.impl_type)
                reg.init_plan_built = True

            if reg.init_plan is None:  # trivial object.__init__
                instance = reg.impl_type()
            else:
                kwargs: dict[str, Any] = {}
                for param_name, param_type, has_default in reg.init_plan:
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

    def _build_init_plan(self, impl_type: type) -> list[tuple[str, Any, bool]] | None:
        """Parse an ``__init__`` into a cached injection plan (called once per registration).

        Returns ``None`` for a trivial ``object.__init__``; otherwise a list of
        ``(param_name, resolved_type, has_default)``. This isolates the expensive
        ``typing.get_type_hints`` + ``inspect.signature`` work so it runs at most once per
        bean instead of on every resolution (mature-container practice).
        """
        init = impl_type.__init__  # type: ignore[misc]
        if init is object.__init__:
            return None
        hints = typing.get_type_hints(init, include_extras=True)
        hints.pop("return", None)
        sig = inspect.signature(init)
        plan: list[tuple[str, Any, bool]] = []
        for param_name, param_type in hints.items():
            param = sig.parameters.get(param_name)
            has_default = param is not None and param.default is not inspect.Parameter.empty
            plan.append((param_name, param_type, has_default))
        return plan

    def _resolve_param(self, param_type: type) -> Any:
        """Resolve a single parameter, handling Annotated, Optional, list, Provider, Map, generics.

        ``get_origin`` is computed once and the plain-class dependency (the common case) is
        fast-pathed, instead of re-running ``get_origin`` for each special form.
        """
        origin = get_origin(param_type)

        # Fast path: a plain class dependency — no typing origin and not a PEP 604 union.
        if origin is None and not isinstance(param_type, types.UnionType):
            # `type` and `Any` are not injectable dependencies — let the caller fall back
            # to the parameter's default (NoSuchBeanError + has_default => use default).
            # `Any` must NOT be resolved: it would match whatever bean happens to be
            # registered under `Any` (e.g. an `@bean ... -> Any`), injecting the wrong
            # object — e.g. a CacheHealthIndicator landing in `registry: Any = None`.
            if param_type is type or param_type is Any:
                raise NoSuchBeanError(bean_type=None)
            return self.resolve(param_type)

        # Annotated[T, Qualifier("name")] / Annotated[T, Value("${key}")]
        if origin is Annotated:
            from pyfly.core.value import Value

            args = get_args(param_type)
            base_type = args[0]
            for metadata in args[1:]:
                if isinstance(metadata, Qualifier):
                    return self.resolve_by_name(metadata.name, expected_type=base_type)
                if isinstance(metadata, Value):
                    return _coerce_value(metadata.resolve(self._get_config()), base_type)
            return self._resolve_param(base_type)

        # Optional[T] (Union[T, None] or T | None via PEP 604)
        if origin is Union or isinstance(param_type, types.UnionType):
            args = get_args(param_type)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                # Optional[Any] (`Any | None`) is not an injectable dependency — `Any`
                # would match whatever bean happens to be registered under `Any`,
                # injecting the wrong object. Leave it unset (None).
                if non_none[0] is Any:
                    return None
                # The inner type goes through this same resolver, not resolve(): a parametrized
                # generic (async_sessionmaker[AsyncSession], Provider[X], Repository[U, ID]),
                # list[X] or Annotated[X, Qualifier(...)] is never a registration key, so a raw
                # lookup always failed and the parameter silently received None.
                try:
                    return self._resolve_param(non_none[0])
                except (NoSuchBeanError, NoUniqueBeanError):
                    return None

        # list[T]
        if origin is list:
            args = get_args(param_type)
            if args:
                return self.resolve_all(args[0])

        # Provider[T] — deferred / fresh resolution (Spring ObjectFactory)
        if origin is Provider:
            args = get_args(param_type)
            if args:
                return Provider(self, args[0])

        # dict[str, T] — Map injection (bean-name -> bean), like Spring Map<String,T>
        if origin is dict:
            args = get_args(param_type)
            if len(args) == 2 and args[0] is str:
                return self._resolve_map(args[1])

        # type[T] or bare `type` — class references cannot be auto-resolved
        if param_type is type or origin is type:
            raise NoSuchBeanError(
                bean_type=param_type if isinstance(param_type, type) else None,
            )

        # Parametrized generic interface, e.g. Repository[User] -> the impl parametrized with
        # User (Spring's generic-aware injection); falls back to the bare origin.
        if isinstance(origin, type) and origin not in (list, set, frozenset, tuple, dict):
            resolved = self._resolve_generic(origin, get_args(param_type))
            if resolved is not None:
                return resolved
            return self.resolve(origin)

        return self.resolve(param_type)

    def _inject_autowired_fields(self, instance: Any) -> None:
        """Inject dependencies into fields marked with Autowired() or Value().

        Only the annotations of those fields are read, each on its own. A class that declares none
        (every third-party @bean product, such as ``AsyncSession``) is left alone, and an annotation
        elsewhere in the class that cannot be resolved (a ``TYPE_CHECKING``-only import) no longer
        disables the injection of the others. A required ``Autowired`` field whose own annotation
        cannot be resolved fails the creation instead of keeping its sentinel.
        """
        from pyfly.core.value import Value

        cls = type(instance)
        for attr_name, owner, default in _injected_fields(cls):
            # Handle @Value("${key}") field descriptors: the expression, not the annotation, decides.
            if isinstance(default, Value):
                from pyfly.core.config import Config

                config_reg = self._registrations.get(Config)
                if config_reg is None or config_reg.instance is None:
                    raise RuntimeError(
                        f"Cannot resolve @Value for {cls.__qualname__}.{attr_name}: Config bean not registered"
                    )
                setattr(instance, attr_name, default.resolve(config_reg.instance))
                continue

            try:
                attr_type = _field_hint(owner, attr_name)
            except Exception as exc:  # noqa: BLE001 — any failure to evaluate the annotation
                if default.required:
                    raise BeanCreationException(
                        subsystem="injection",
                        provider=f"{cls.__qualname__}.{attr_name}",
                        reason=(
                            f"the annotation of the Autowired field {cls.__qualname__}.{attr_name} "
                            f"cannot be resolved ({type(exc).__name__}: {exc}); import the type at runtime"
                        ),
                    ) from exc
                logging.getLogger(__name__).warning(
                    "The annotation of the optional Autowired field %s.%s cannot be resolved (%s: %s); it is left None",
                    cls.__qualname__,
                    attr_name,
                    type(exc).__name__,
                    exc,
                )
                setattr(instance, attr_name, None)
                continue

            try:
                if default.qualifier:
                    base = get_args(attr_type)[0] if get_origin(attr_type) is Annotated else attr_type
                    value = self.resolve_by_name(default.qualifier, expected_type=base)
                else:
                    value = self._resolve_param(attr_type)
            except (NoSuchBeanError, NoUniqueBeanError):
                if default.required and default.qualifier:
                    raise
                if default.required:
                    raise NoSuchBeanError(
                        bean_type=attr_type if isinstance(attr_type, type) else None,
                        required_by=f"{cls.__qualname__}.{attr_name}",
                        parameter=f"{attr_name}: {getattr(attr_type, '__name__', repr(attr_type))} = Autowired()",
                    ) from None
                value = None

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
