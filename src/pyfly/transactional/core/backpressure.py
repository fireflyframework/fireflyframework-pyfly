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
"""Backpressure strategies for saga / workflow layer execution.

Mirrors ``org.fireflyframework.orchestration.core.backpressure`` —
adaptive (semaphore-bounded), batched, and circuit-breaker variants.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pyfly.transactional.core.model import BackpressureProperties, CircuitBreakerConfig


@runtime_checkable
class BackpressureStrategy(Protocol):
    """SPI for layer-execution backpressure strategies."""

    name: str

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]: ...


class AdaptiveBackpressureStrategy:
    """Bounded concurrency via :class:`asyncio.Semaphore` — the default."""

    name = "adaptive"

    def __init__(self, concurrency: int = 10) -> None:
        self._concurrency = max(1, concurrency)

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def _bounded(item: Any) -> Any:
            async with semaphore:
                return await processor(item)

        return await asyncio.gather(*(_bounded(i) for i in items), return_exceptions=False)


class BatchedBackpressureStrategy:
    """Splits items into fixed-size batches; each batch runs concurrently."""

    name = "batched"

    def __init__(self, batch_size: int = 10) -> None:
        self._batch_size = max(1, batch_size)

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        results: list[Any] = []
        for i in range(0, len(items), self._batch_size):
            chunk = items[i : i + self._batch_size]
            results.extend(await asyncio.gather(*(processor(c) for c in chunk)))
        return results


class CircuitBreakerBackpressureStrategy:
    """Trips after *failure_threshold* failures; auto-recovers after timeout."""

    name = "circuit-breaker"

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        results: list[Any] = []
        for item in items:
            await self._check_state()
            try:
                value = await processor(item)
                await self._on_success()
                results.append(value)
            except Exception:  # noqa: BLE001
                await self._on_failure()
                raise
        return results

    async def _check_state(self) -> None:
        async with self._lock:
            if self._opened_at is None:
                return
            elapsed_ms = (time.monotonic() - self._opened_at) * 1000.0
            if elapsed_ms < self._config.recovery_timeout_ms:
                msg = "circuit breaker OPEN"
                raise RuntimeError(msg)
            if self._half_open_calls >= self._config.half_open_max_calls:
                msg = "circuit breaker half-open quota exhausted"
                raise RuntimeError(msg)
            self._half_open_calls += 1

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open_calls = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._config.failure_threshold:
                self._opened_at = time.monotonic()


def make_strategy(props: BackpressureProperties) -> BackpressureStrategy:
    """Resolve a strategy by name from a :class:`BackpressureProperties`."""
    name = props.strategy.lower()
    if name in {"adaptive", "default", ""}:
        return AdaptiveBackpressureStrategy(concurrency=max(props.batch_size, 1))
    if name == "batched":
        return BatchedBackpressureStrategy(batch_size=props.batch_size)
    if name in {"circuit-breaker", "circuit_breaker"}:
        return CircuitBreakerBackpressureStrategy(props.circuit_breaker)
    msg = f"unknown backpressure strategy: {props.strategy}"
    raise ValueError(msg)
