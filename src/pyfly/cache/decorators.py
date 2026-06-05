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
"""Declarative caching decorators."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from pyfly.cache.ports.outbound import CacheAdapter

F = TypeVar("F", bound=Callable[..., Any])


def _require_async(func: Callable[..., Any], decorator_name: str) -> None:
    """Reject a sync target with a clear error at decoration time.

    Cache adapters are async, so the wrappers must ``await`` the backend; a sync
    target would otherwise fail with a cryptic ``await`` ``TypeError`` at call
    time. Make a synchronous function an explicit, immediate error instead.
    """
    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"{decorator_name} requires an async function; "
            f"'{func.__qualname__}' is synchronous (cache adapters are async-only)."
        )


def _resolve_key(func: Callable[..., Any], key: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Resolve a ``{param}`` key template against the call's bound arguments.

    The cache key must uniquely identify the value *within its backend*: the
    backend instance plus this key form the cache namespace. Reuse the same
    template across methods only when they refer to the same logical entry — that
    is what lets a ``@cache_evict`` invalidate a ``@cacheable`` entry; two
    unrelated methods sharing one backend must use distinct templates.
    """
    bound = inspect.signature(func).bind(*args, **kwargs)
    bound.apply_defaults()
    try:
        return key.format(**bound.arguments)
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"Cache key template {key!r} for '{func.__qualname__}' references unknown parameter {exc}."
        ) from exc


def cache(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
) -> Callable[[F], F]:
    """Cache the return value of an async function.

    The `key` parameter supports format-string interpolation with function
    argument names. For example, `key="user:{user_id}"` will expand
    `{user_id}` from the function's arguments.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache/@cacheable")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved_key = _resolve_key(func, key, args, kwargs)

            # Check cache. A present-but-None entry is a hit (null caching /
            # cache-penetration protection), distinguished via exists (audit #80).
            cached = await backend.get(resolved_key)
            if cached is not None:
                return cached
            if await backend.exists(resolved_key):
                return None

            # Execute and cache
            result = await func(*args, **kwargs)
            await backend.put(resolved_key, result, ttl=ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def cacheable(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
) -> Callable[[F], F]:
    """Cache the return value, skip execution on cache hit.

    Equivalent to :func:`cache`.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
    """
    return cache(backend=backend, key=key, ttl=ttl)


def cache_evict(
    backend: CacheAdapter,
    key: str = "",
    all_entries: bool = False,
) -> Callable[[F], F]:
    """Evict a cache entry (or all entries) after method execution.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders. Ignored when *all_entries* is ``True``.
        all_entries: When ``True``, clear the entire cache after execution.
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache_evict")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            if all_entries:
                await backend.clear()
            else:
                await backend.evict(_resolve_key(func, key, args, kwargs))
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def cache_put(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
) -> Callable[[F], F]:
    """Always execute the method and cache the result.

    Unlike :func:`cacheable`, the decorated function is always invoked.
    This is useful for update operations where you want to refresh the
    cached value.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache_put")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            await backend.put(_resolve_key(func, key, args, kwargs), result, ttl=ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
