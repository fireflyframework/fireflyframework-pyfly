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
"""BeanPostProcessor that wires declarative @http_client / @service_client beans."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from pyfly.client.circuit_breaker import CircuitBreaker
from pyfly.client.retry import RetryPolicy


class HttpClientBeanPostProcessor:
    """Replaces stub methods on @http_client / @service_client beans with real HTTP calls.

    For each method with __pyfly_http_method__ metadata, generates an
    implementation that delegates to an HttpClientPort instance.  When
    the class carries ``__pyfly_resilience__`` metadata (set by
    ``@service_client``), each generated method is wrapped with a
    :class:`CircuitBreaker` and/or :class:`RetryPolicy`.
    """

    def __init__(
        self,
        http_client_factory: Callable[[str], Any] | None = None,
        *,
        default_retry: dict[str, Any] | None = None,
        default_circuit_breaker: dict[str, Any] | None = None,
    ) -> None:
        self._factory = http_client_factory or self._default_factory
        self._clients: dict[str, Any] = {}
        self._default_retry = default_retry or {"max-attempts": 3, "base-delay": 1.0}
        self._default_circuit_breaker = default_circuit_breaker or {
            "failure-threshold": 5,
            "recovery-timeout": 30,
        }

    @staticmethod
    def _default_factory(base_url: str) -> Any:
        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

        return HttpxClientAdapter(base_url=base_url)

    def before_init(self, bean: Any, bean_name: str) -> Any:
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        cls = type(bean)
        if not getattr(cls, "__pyfly_http_client__", False):
            return bean

        # Skip if another post-processor already wired this bean
        if getattr(bean, "__pyfly_http_wired__", False):
            return bean

        base_url = getattr(cls, "__pyfly_http_base_url__", "")
        client = self._factory(base_url)
        self._clients[bean_name] = client

        # Build per-service resilience wrappers
        resilience = getattr(cls, "__pyfly_resilience__", {})
        cb = self._build_circuit_breaker(resilience)
        retry = self._build_retry_policy(resilience)

        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            http_method = getattr(attr, "__pyfly_http_method__", None)
            if http_method is None:
                continue
            http_path = getattr(attr, "__pyfly_http_path__", "")
            impl = self._make_method_impl(client, http_method, http_path, attr)
            impl = self._wrap_with_resilience(impl, cb, retry)
            setattr(bean, attr_name, impl.__get__(bean, cls))

        bean.__pyfly_http_wired__ = True
        return bean

    # ------------------------------------------------------------------
    # Resilience helpers
    # ------------------------------------------------------------------

    def _build_circuit_breaker(self, resilience: dict[str, Any]) -> CircuitBreaker | None:
        if not resilience.get("circuit_breaker", False):
            return None
        threshold = (
            resilience.get("circuit_breaker_failure_threshold") or self._default_circuit_breaker["failure-threshold"]
        )
        timeout = (
            resilience.get("circuit_breaker_recovery_timeout") or self._default_circuit_breaker["recovery-timeout"]
        )
        return CircuitBreaker(
            failure_threshold=int(threshold),
            recovery_timeout=timedelta(seconds=float(timeout)),
        )

    def _build_retry_policy(self, resilience: dict[str, Any]) -> RetryPolicy | None:
        retry_val = resilience.get("retry", False)
        if not retry_val:
            return None
        if isinstance(retry_val, int) and not isinstance(retry_val, bool):
            max_attempts = retry_val
        else:
            max_attempts = int(self._default_retry["max-attempts"])
        base_delay = float(resilience.get("retry_base_delay") or self._default_retry["base-delay"])
        return RetryPolicy(
            max_attempts=max_attempts,
            base_delay=timedelta(seconds=base_delay),
        )

    @staticmethod
    def _wrap_with_resilience(
        impl: Any,
        cb: CircuitBreaker | None,
        retry: RetryPolicy | None,
    ) -> Any:
        """Wrap a generated method impl: retry outside, circuit breaker inside."""
        if cb is None and retry is None:
            return impl

        original = impl

        async def resilient_impl(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            async def call() -> Any:
                if cb is not None:
                    return await cb.call(original, self_arg, *args, **kwargs)
                return await original(self_arg, *args, **kwargs)

            if retry is not None:
                return await retry.execute(call)
            return await call()

        return resilient_impl

    # ------------------------------------------------------------------
    # Method implementation (unchanged)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_method_impl(client: Any, http_method: str, path_template: str, original: Any) -> Any:
        sig = inspect.signature(original)

        async def implementation(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(self_arg, *args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
            params.pop("self", None)

            # Resolve path variables
            path = path_template
            path_vars: set[str] = set()
            for key, value in params.items():
                placeholder = f"{{{key}}}"
                if placeholder in path:
                    path = path.replace(placeholder, str(value))
                    path_vars.add(key)

            remaining = {k: v for k, v in params.items() if k not in path_vars}

            request_kwargs: dict[str, Any] = {}
            if http_method in ("POST", "PUT", "PATCH") and "body" in remaining:
                request_kwargs["json"] = remaining.pop("body")
            if remaining:
                request_kwargs["params"] = remaining

            response = await client.request(http_method, path, **request_kwargs)
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return response.text

        return implementation
