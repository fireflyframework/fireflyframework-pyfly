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
"""Declarative caching decorators.

The decorators are transaction-aware (Spring's ``TransactionAwareCacheDecorator``): inside a unit of work
their puts and evictions wait for the commit and are dropped on rollback, see
:class:`~pyfly.cache.transaction.TransactionAwareCache`. Outside one they run at once.

A cache failure never changes the outcome of the decorated call: the method has run (and may have
committed), so a put or an eviction that fails is logged (logger ``pyfly.cache``) and the result is
returned. A failing read, before the method runs, propagates.

The declared return type is checked when the method is decorated: a type the cache cannot hold (an
ORM-mapped class or a Beanie document, also as ``list[Order]`` or ``Order | None``) raises ``TypeError``
there. A forward reference that cannot be resolved yet is checked at the first call, before the method
runs. On a hit the value is rebuilt as the declared type
(:func:`~pyfly.cache.serialization.restore`), so a JSON backend (Redis, PostgreSQL) returns a Pydantic
model or a dataclass, not a ``dict``.
"""

from __future__ import annotations

import functools
import inspect
import logging
import typing
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from pyfly.cache.namespaces import cache_region
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import restore, uncacheable_type
from pyfly.cache.transaction import TransactionAwareCache

F = TypeVar("F", bound=Callable[..., Any])

_logger = logging.getLogger("pyfly.cache")


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


def _resolve_key(
    func: Callable[..., Any],
    key: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    signature: inspect.Signature | None = None,
) -> str:
    """Resolve a ``{param}`` key template against the call's bound arguments.

    The cache key must uniquely identify the value *within its backend*: the
    backend instance plus this key form the cache namespace. Reuse the same
    template across methods only when they refer to the same logical entry — that
    is what lets a ``@cache_evict`` invalidate a ``@cacheable`` entry; two
    unrelated methods sharing one backend must use distinct templates (or distinct
    ``cache_name`` regions).
    """
    bound = (signature or inspect.signature(func)).bind(*args, **kwargs)
    bound.apply_defaults()
    try:
        return key.format(**bound.arguments)
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"Cache key template {key!r} for '{func.__qualname__}' references unknown parameter {exc}."
        ) from exc


def _target(backend: CacheAdapter, cache_name: str | None) -> TransactionAwareCache:
    region: CacheAdapter = backend if cache_name is None else cache_region(backend, cache_name)
    return TransactionAwareCache(region, on_write_error="log")


class _ReturnType:
    """The declared return type of a decorated function: checked once, and used to rebuild hits."""

    __slots__ = ("_annotation", "_decorator", "_func", "_refusal", "_resolved", "_warned")

    def __init__(self, func: Callable[..., Any], decorator: str) -> None:
        self._func = func
        self._decorator = decorator
        self._annotation: Any = Any
        self._refusal: TypeError | None = None
        self._resolved = False
        self._warned = False
        self._resolve(at_call=False)
        if self._refusal is not None:
            raise self._refusal

    def _resolve(self, *, at_call: bool) -> None:
        try:
            hints = typing.get_type_hints(self._func)
        except Exception:  # noqa: BLE001 — a forward reference not defined yet: resolve at the first call
            if not at_call:
                return
            hints = {}
        self._resolved = True
        annotation = hints.get("return", Any)
        reason = uncacheable_type(annotation)
        if reason is not None:
            self._refusal = TypeError(
                f"{self._decorator} on '{self._func.__qualname__}' cannot cache its declared return type: it is "
                f"{reason}. A cached ORM object would be shared across requests and outlive its session; return "
                "a DTO (a Pydantic model, a dataclass or a dict) from the cached method instead."
            )
            return
        self._annotation = annotation

    def check(self) -> None:
        """Raise the refusal of the declared type, resolving it now if decoration could not."""
        if not self._resolved:
            self._resolve(at_call=True)
        if self._refusal is not None:
            raise self._refusal

    def restore(self, value: Any, key: str) -> tuple[bool, Any]:
        """``(True, hit)`` with *value* rebuilt as the declared type, or ``(False, None)`` when the stored
        value no longer fits it (the entry is then treated as a miss and overwritten)."""
        try:
            return True, restore(value, self._annotation)
        except Exception as error:  # noqa: BLE001 — pydantic.ValidationError: a stale entry is a miss
            if not self._warned:
                self._warned = True
                _logger.warning(
                    "cache_hit_discarded key=%r function=%s: the cached value does not fit the declared return "
                    "type: %s",
                    key,
                    self._func.__qualname__,
                    error,
                )
            return False, None


def cache(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
    *,
    condition: Callable[..., bool] | None = None,
    unless: Callable[[Any], bool] | None = None,
    cache_name: str | None = None,
) -> Callable[[F], F]:
    """Cache the return value of an async function.

    The `key` parameter supports format-string interpolation with function
    argument names. For example, `key="user:{user_id}"` will expand
    `{user_id}` from the function's arguments.

    Inside a unit of work the value is stored after the commit, and not at all when the unit rolls back
    (a value read inside a transaction may be one that never commits).

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
        condition: Predicate over the call arguments (same signature as *func*); when it
            returns ``False`` caching is bypassed entirely (the function runs, nothing is
            read from or written to the cache). Spring's ``@Cacheable(condition=...)``.
        unless: Predicate over the *result*; when it returns ``True`` the result is returned
            but NOT stored. Spring's ``@Cacheable(unless=...)``.
        cache_name: A named region of *backend* (keys under ``<cache_name>::``) that
            ``@cache_evict(all_entries=True, cache_name=...)`` clears on its own. Spring's ``cacheNames``.

    Raises:
        TypeError: When decorating, for a sync function or a declared return type the cache cannot hold.
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache/@cacheable")
        returns = _ReturnType(func, "@cacheable")
        signature = inspect.signature(func)
        target = _target(backend, cache_name)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # condition=False -> bypass the cache entirely.
            if condition is not None and not condition(*args, **kwargs):
                return await func(*args, **kwargs)

            returns.check()
            resolved_key = _resolve_key(func, key, args, kwargs, signature)

            # Check cache. A present-but-None entry is a hit (null caching /
            # cache-penetration protection), distinguished via exists (audit #80).
            cached = await target.get(resolved_key)
            if cached is not None:
                fits, value = returns.restore(cached, resolved_key)
                if fits:
                    return value
            elif await target.exists(resolved_key):
                return None

            # Execute and (unless excluded) cache: after the commit inside a unit, never raising.
            result = await func(*args, **kwargs)
            if unless is None or not unless(result):
                await target.put(resolved_key, result, ttl=ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def cacheable(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
    *,
    condition: Callable[..., bool] | None = None,
    unless: Callable[[Any], bool] | None = None,
    cache_name: str | None = None,
) -> Callable[[F], F]:
    """Cache the return value, skip execution on cache hit.

    Equivalent to :func:`cache`, with Spring-style ``condition`` (bypass caching) and
    ``unless`` (don't store certain results) predicates.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
        condition: Predicate over the call arguments; ``False`` bypasses the cache.
        unless: Predicate over the result; ``True`` returns it without storing.
        cache_name: A named region of *backend* (see :func:`cache`).
    """
    return cache(backend=backend, key=key, ttl=ttl, condition=condition, unless=unless, cache_name=cache_name)


def cache_evict(
    backend: CacheAdapter,
    key: str = "",
    all_entries: bool = False,
    *,
    before_invocation: bool = False,
    cache_name: str | None = None,
) -> Callable[[F], F]:
    """Evict a cache entry (or all entries) after method execution.

    Inside a unit of work the eviction runs after the commit, and not at all when the unit rolls back: an
    eviction before the commit would let a concurrent reader re-cache the old value for good.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders. Ignored when *all_entries* is ``True``.
        all_entries: When ``True``, clear the cache after execution: the *cache_name* region when one is
            named, otherwise *backend* itself (its own entries, never a whole shared store).
        before_invocation: Evict at once, before the method runs, even inside a unit of work (Spring's
            ``beforeInvocation``). A failure then propagates, since nothing has run yet.
        cache_name: A named region of *backend* (see :func:`cache`).
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache_evict")
        signature = inspect.signature(func)
        target = _target(backend, cache_name)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved_key = None if all_entries else _resolve_key(func, key, args, kwargs, signature)
            if before_invocation:
                if resolved_key is None:
                    await target.invalidate()
                else:
                    await target.evict_if_present(resolved_key)
                return await func(*args, **kwargs)
            result = await func(*args, **kwargs)
            if resolved_key is None:
                await target.clear()
            else:
                await target.evict(resolved_key)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def cache_put(
    backend: CacheAdapter,
    key: str,
    ttl: timedelta | None = None,
    *,
    cache_name: str | None = None,
) -> Callable[[F], F]:
    """Always execute the method and cache the result.

    Unlike :func:`cacheable`, the decorated function is always invoked.
    This is useful for update operations where you want to refresh the
    cached value. Inside a unit of work the value is stored after the commit, and not at all when the
    unit rolls back.

    Args:
        backend: Cache adapter to use.
        key: Key template with {param} placeholders.
        ttl: Optional time-to-live for cached entries.
        cache_name: A named region of *backend* (see :func:`cache`).

    Raises:
        TypeError: When decorating, for a sync function or a declared return type the cache cannot hold.
    """

    def decorator(func: F) -> F:
        _require_async(func, "@cache_put")
        returns = _ReturnType(func, "@cache_put")
        signature = inspect.signature(func)
        target = _target(backend, cache_name)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            returns.check()
            resolved_key = _resolve_key(func, key, args, kwargs, signature)
            result = await func(*args, **kwargs)
            await target.put(resolved_key, result, ttl=ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
