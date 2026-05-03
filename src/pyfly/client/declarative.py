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
"""Declarative HTTP client — Spring-style @service_client / @http_client with @get/@post etc."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.container.types import Scope

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")


def service_client(
    base_url: str,
    *,
    retry: int | bool = True,
    circuit_breaker: bool = True,
    retry_base_delay: float | None = None,
    circuit_breaker_failure_threshold: int | None = None,
    circuit_breaker_recovery_timeout: float | None = None,
) -> Callable[[type[T]], type[T]]:
    """Mark a class as a resilient declarative HTTP client.

    Methods decorated with ``@get``, ``@post``, etc. will have implementations
    generated at startup by ``HttpClientBeanPostProcessor``, wrapped with
    circuit breaker and retry logic.

    Args:
        base_url: Base URL for all requests.
        retry: ``True`` for default retry, an ``int`` for max attempts, or
            ``False`` to disable.
        circuit_breaker: Enable circuit breaker wrapping.
        retry_base_delay: Override the default base delay (seconds) between
            retries.
        circuit_breaker_failure_threshold: Override failures before opening.
        circuit_breaker_recovery_timeout: Override recovery timeout (seconds).
    """

    def decorator(cls: type[T]) -> type[T]:
        cls.__pyfly_http_client__ = True  # type: ignore[attr-defined]
        cls.__pyfly_http_base_url__ = base_url  # type: ignore[attr-defined]
        cls.__pyfly_injectable__ = True  # type: ignore[attr-defined]
        cls.__pyfly_stereotype__ = "component"  # type: ignore[attr-defined]
        cls.__pyfly_scope__ = Scope.SINGLETON  # type: ignore[attr-defined]
        cls.__pyfly_service_client__ = True  # type: ignore[attr-defined]
        cls.__pyfly_resilience__ = {  # type: ignore[attr-defined]
            "retry": retry,
            "circuit_breaker": circuit_breaker,
            "retry_base_delay": retry_base_delay,
            "circuit_breaker_failure_threshold": circuit_breaker_failure_threshold,
            "circuit_breaker_recovery_timeout": circuit_breaker_recovery_timeout,
        }
        return cls

    return decorator


def http_client(base_url: str) -> Callable[[type[T]], type[T]]:
    """Mark a class as a declarative HTTP client (no resilience).

    Equivalent to ``@service_client(base_url, retry=False, circuit_breaker=False)``.
    Prefer ``@service_client`` for production use.
    """
    return service_client(base_url, retry=False, circuit_breaker=False)


def _make_http_method_decorator(method: str) -> Callable[[str], Callable[[F], F]]:
    """Factory for HTTP method decorators (@get, @post, etc.)."""

    def method_decorator(path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            @functools.wraps(func)
            async def placeholder(*args: Any, **kwargs: Any) -> Any:
                raise NotImplementedError(
                    f"{func.__qualname__} has not been wired by "
                    "HttpClientBeanPostProcessor. "
                    "Make sure the ApplicationContext is started."
                )

            placeholder.__pyfly_http_method__ = method  # type: ignore[attr-defined]
            placeholder.__pyfly_http_path__ = path  # type: ignore[attr-defined]
            return placeholder  # type: ignore[return-value]

        return decorator

    return method_decorator


get = _make_http_method_decorator("GET")
post = _make_http_method_decorator("POST")
put = _make_http_method_decorator("PUT")
delete = _make_http_method_decorator("DELETE")
patch = _make_http_method_decorator("PATCH")
