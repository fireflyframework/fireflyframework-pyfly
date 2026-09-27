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
"""Scoped proxies — Spring's ``ScopedProxyMode.TARGET_CLASS`` for request-, session- and refresh-scoped beans.

A bean of a narrower scope injected into a singleton is resolved once, when the singleton is built:
the singleton keeps that one instance forever. After a refresh it keeps the evicted datasource (the
old host, the old password), and it cannot take a request-scoped bean at all, because there is no
request at startup. A scoped proxy fixes both: the container injects a proxy, and every use of the
proxy (an attribute, a call, ``async with``) resolves the instance the scope holds at that moment.

Opt in per bean, with ``@refresh_scope(proxy=True)`` on a class or ``@scoped_proxy`` on a class or on a
non-singleton ``@bean`` method. ``isinstance(proxy, Target)`` is true; ``type(proxy)`` is
:class:`ScopedProxy`, and :func:`proxy_target` returns the current instance. ``Provider[T]`` is the
explicit alternative: ``provider.get()`` resolves the current instance too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ScopedProxy:
    """Stands in for a scoped bean and forwards every use to the instance its scope currently holds."""

    __slots__ = ("__pyfly_resolve__", "__pyfly_target_type__", "__weakref__")

    def __init__(self, resolve: Callable[[], Any], target_type: type) -> None:
        object.__setattr__(self, "__pyfly_resolve__", resolve)
        object.__setattr__(self, "__pyfly_target_type__", target_type)

    # ``isinstance(proxy, Target)`` and ``proxy.__class__`` answer for the target, as Spring's CGLIB
    # proxies (a subclass of the target) do.
    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return object.__getattribute__(self, "__pyfly_target_type__")  # type: ignore[no-any-return]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            # Dunder and framework-marker lookups (``__pyfly_scheduled__``, ``__wrapped__``) describe the
            # proxy, not the instance: forwarding them would create a scoped instance while the context
            # scans its beans (and fail outside a request for a request-scoped one).
            raise AttributeError(name)
        return getattr(_current(self), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_current(self), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_current(self), name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _current(self)(*args, **kwargs)

    def __repr__(self) -> str:
        target_type = object.__getattribute__(self, "__pyfly_target_type__")
        return f"<ScopedProxy for {getattr(target_type, '__qualname__', target_type)}>"

    def __bool__(self) -> bool:
        return bool(_current(self))

    def __len__(self) -> int:
        return len(_current(self))

    def __iter__(self) -> Any:
        return iter(_current(self))

    def __contains__(self, item: object) -> bool:
        return item in _current(self)

    def __getitem__(self, key: Any) -> Any:
        return _current(self)[key]

    def __enter__(self) -> Any:
        return _current(self).__enter__()

    def __exit__(self, *exc_info: Any) -> Any:
        return _current(self).__exit__(*exc_info)

    async def __aenter__(self) -> Any:
        return await _current(self).__aenter__()

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await _current(self).__aexit__(*exc_info)


def _current(proxy: ScopedProxy) -> Any:
    return object.__getattribute__(proxy, "__pyfly_resolve__")()


def is_scoped_proxy(candidate: Any) -> bool:
    """Whether *candidate* is a :class:`ScopedProxy` (``isinstance`` would answer for its target)."""
    return type(candidate) is ScopedProxy


def proxy_target(candidate: Any) -> Any:
    """The instance *candidate* currently stands for, or *candidate* itself when it is not a proxy."""
    return _current(candidate) if is_scoped_proxy(candidate) else candidate
