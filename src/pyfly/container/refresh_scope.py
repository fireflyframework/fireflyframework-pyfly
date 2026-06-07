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
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

#: The custom-scope name under which the RefreshScope handler is registered.
REFRESH_SCOPE_NAME = "refresh"

_MISSING = object()  # distinct from None so a bean that legitimately returns None still caches

F = TypeVar("F", bound=type)


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
        """Evict every cached refresh-scoped instance; returns the evicted cache keys."""
        with self._lock:
            keys = list(self._cache)
            self._cache.clear()
            return keys


def refresh_scope(cls: F) -> F:
    """Mark a bean as refresh-scoped (``scope="refresh"``). Compose with a stereotype, e.g.::

    @component
    @refresh_scope
    class FeatureFlags: ...
    """
    cls.__pyfly_scope__ = REFRESH_SCOPE_NAME  # type: ignore[attr-defined]
    return cls
