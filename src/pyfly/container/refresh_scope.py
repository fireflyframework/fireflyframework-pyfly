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
"""Refresh scope — Spring Cloud's ``@RefreshScope``.

A ``RefreshScope`` bean is cached like a singleton, but a refresh (via
``ContextRefresher.refresh()`` or ``POST /actuator/refresh``) evicts every refresh-scoped
instance so the next resolution rebuilds it — re-running constructor/field injection and
re-reading ``@Value`` placeholders against the live ``Config``.

An evicted instance is destroyed after the swap (its ``@pre_destroy`` runs), so a refresh-scoped
bean that owns an engine disposes it; ``ApplicationContext.stop()`` destroys the cached ones. A
singleton that injects a refresh-scoped bean keeps the instance it received unless the bean is
declared with ``@refresh_scope(proxy=True)`` (see :mod:`pyfly.container.scoped_proxy`) or the
singleton injects ``Provider[T]``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar, overload

from pyfly.container.types import Scope

#: The custom-scope name under which the RefreshScope handler is registered.
REFRESH_SCOPE_NAME = "refresh"

_MISSING = object()  # distinct from None so a bean that legitimately returns None still caches

F = TypeVar("F", bound=type)
T = TypeVar("T")


class RefreshScope:
    """A thread-safe :class:`~pyfly.container.types.ScopeHandler` that caches instances until
    :meth:`refresh` evicts them all."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()

    def get(self, name: str, object_factory: Callable[[], Any]) -> Any:
        cached = self._cache.get(name, _MISSING)
        if cached is not _MISSING:
            return cached
        with self._lock:  # double-checked create, mirroring the container's SINGLETON path
            cached = self._cache.get(name, _MISSING)
            if cached is not _MISSING:
                return cached
            instance = object_factory()
            self._cache[name] = instance
            return instance

    def remove(self, name: str) -> Any | None:
        with self._lock:
            value = self._cache.pop(name, _MISSING)
            return None if value is _MISSING else value

    def refresh(self) -> list[str]:
        """Evict every cached refresh-scoped instance; returns the evicted cache keys.

        The instances are dropped, not destroyed: :meth:`evict_all` hands them back so the caller can
        destroy them, which is what ``ContextRefresher.refresh()`` does.
        """
        return list(self.evict_all())

    def evict_all(self) -> dict[str, Any]:
        """Evict every cached instance and return them by cache key, for the caller to destroy.

        The optional destruction hook of the :class:`~pyfly.container.types.ScopeHandler` SPI: the
        context calls it on refresh and on stop.
        """
        with self._lock:
            evicted = dict(self._cache)
            self._cache.clear()
            return evicted


@overload
def refresh_scope(cls: F) -> F: ...


@overload
def refresh_scope(*, proxy: bool = False) -> Callable[[F], F]: ...


def refresh_scope(cls: F | None = None, *, proxy: bool = False) -> F | Callable[[F], F]:
    """Mark a bean as refresh-scoped (``scope="refresh"``). Compose with a stereotype, in either order::

        @refresh_scope
        @component
        class FeatureFlags: ...

    ``proxy=True`` injects a scoped proxy instead of the instance, so a singleton that depends on
    the bean follows every refresh (Spring Cloud proxies refresh-scoped beans by default; here it is
    opt-in)::

        @refresh_scope(proxy=True)
        @component
        class ReportingDataSource: ...

    A stereotype applied after this decorator (written above it) used to reset the scope to
    singleton; the refresh scope now survives it.
    """

    def decorator(target: F) -> F:
        target.__pyfly_scope__ = REFRESH_SCOPE_NAME  # type: ignore[attr-defined]
        target.__pyfly_refresh_scope__ = True  # type: ignore[attr-defined]
        if proxy:
            target.__pyfly_scoped_proxy__ = True  # type: ignore[attr-defined]
        return target

    if cls is not None:
        return decorator(cls)
    return decorator


def scoped_proxy(target: T) -> T:
    """Inject a scoped proxy for this bean (a class, or a non-singleton ``@bean`` method).

    The bean must have a REQUEST, SESSION or custom scope (``"refresh"``): the proxy resolves the
    instance the scope holds on every use. On a ``@bean`` method, write it above ``@bean``::

        @scoped_proxy
        @bean(scope="refresh")
        def reporting_engine(self, config: Config) -> AsyncEngine: ...

    Raises ``TypeError`` on a singleton or transient ``@bean`` method, which a proxy cannot serve.
    """
    bean_scope = getattr(target, "__pyfly_bean_scope__", None)
    if bean_scope in (Scope.SINGLETON, Scope.TRANSIENT):
        raise TypeError(
            f"a scoped proxy needs a REQUEST, SESSION or custom scope; "
            f"{getattr(target, '__qualname__', target)!r} is {bean_scope.name}"
        )
    target.__pyfly_scoped_proxy__ = True  # type: ignore[attr-defined]
    return target
