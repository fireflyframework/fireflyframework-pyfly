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
"""CQRS handler decorators — enhanced to mirror Java annotations.

Mark handler classes with ``@command_handler`` / ``@query_handler`` for
auto-discovery by the :class:`~pyfly.cqrs.command.registry.HandlerRegistry`.
The decorators accept keyword arguments matching Java's
``@CommandHandlerComponent`` / ``@QueryHandlerComponent`` annotations
(timeout, retries, metrics, tracing, caching, etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

T = TypeVar("T", bound=type)


# ── command_handler ────────────────────────────────────────────


@overload
def command_handler(cls: T) -> T: ...


@overload
def command_handler(
    *,
    timeout: int | None = None,
    retries: int = 0,
    backoff_ms: int = 1000,
    metrics: bool = True,
    tracing: bool = True,
    validation: bool = True,
    priority: int = 0,
    tags: tuple[str, ...] = (),
    description: str = "",
) -> Callable[..., Any]: ...


def command_handler(
    cls: T | None = None,
    *,
    timeout: int | None = None,
    retries: int = 0,
    backoff_ms: int = 1000,
    metrics: bool = True,
    tracing: bool = True,
    validation: bool = True,
    priority: int = 0,
    tags: tuple[str, ...] = (),
    description: str = "",
) -> T | Callable[..., Any]:
    """Mark a class as a command handler for auto-discovery.

    Can be used as a bare decorator or with arguments:

        @command_handler
        class MyHandler: ...

        @command_handler(timeout=30, retries=2)
        class MyHandler: ...
    """

    def _apply(klass: T) -> T:
        klass.__pyfly_handler_type__ = "command"  # type: ignore[attr-defined]
        klass.__pyfly_timeout__ = timeout  # type: ignore[attr-defined]
        klass.__pyfly_retries__ = retries  # type: ignore[attr-defined]
        klass.__pyfly_backoff_ms__ = backoff_ms  # type: ignore[attr-defined]
        klass.__pyfly_metrics__ = metrics  # type: ignore[attr-defined]
        klass.__pyfly_tracing__ = tracing  # type: ignore[attr-defined]
        klass.__pyfly_validation__ = validation  # type: ignore[attr-defined]
        klass.__pyfly_priority__ = priority  # type: ignore[attr-defined]
        klass.__pyfly_tags__ = tags  # type: ignore[attr-defined]
        klass.__pyfly_description__ = description  # type: ignore[attr-defined]
        return klass

    if cls is not None:
        return _apply(cls)
    return _apply


# ── query_handler ──────────────────────────────────────────────


@overload
def query_handler(cls: T) -> T: ...


@overload
def query_handler(
    *,
    timeout: int | None = None,
    retries: int = 0,
    metrics: bool = True,
    tracing: bool = True,
    cacheable: bool = False,
    cache_ttl: int | None = None,
    cache_key_prefix: str | None = None,
    priority: int = 0,
    tags: tuple[str, ...] = (),
    description: str = "",
) -> Callable[..., Any]: ...


def query_handler(
    cls: T | None = None,
    *,
    timeout: int | None = None,
    retries: int = 0,
    metrics: bool = True,
    tracing: bool = True,
    cacheable: bool = False,
    cache_ttl: int | None = None,
    cache_key_prefix: str | None = None,
    priority: int = 0,
    tags: tuple[str, ...] = (),
    description: str = "",
) -> T | Callable[..., Any]:
    """Mark a class as a query handler for auto-discovery.

    Can be used as a bare decorator or with arguments:

        @query_handler
        class MyHandler: ...

        @query_handler(cacheable=True, cache_ttl=600)
        class MyHandler: ...
    """

    def _apply(klass: T) -> T:
        klass.__pyfly_handler_type__ = "query"  # type: ignore[attr-defined]
        klass.__pyfly_timeout__ = timeout  # type: ignore[attr-defined]
        klass.__pyfly_retries__ = retries  # type: ignore[attr-defined]
        klass.__pyfly_metrics__ = metrics  # type: ignore[attr-defined]
        klass.__pyfly_tracing__ = tracing  # type: ignore[attr-defined]
        klass.__pyfly_cacheable__ = cacheable  # type: ignore[attr-defined]
        klass.__pyfly_cache_ttl__ = cache_ttl  # type: ignore[attr-defined]
        klass.__pyfly_cache_key_prefix__ = cache_key_prefix  # type: ignore[attr-defined]
        klass.__pyfly_priority__ = priority  # type: ignore[attr-defined]
        klass.__pyfly_tags__ = tags  # type: ignore[attr-defined]
        klass.__pyfly_description__ = description  # type: ignore[attr-defined]
        return klass

    if cls is not None:
        return _apply(cls)
    return _apply
