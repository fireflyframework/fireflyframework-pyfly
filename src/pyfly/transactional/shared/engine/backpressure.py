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
"""Backpressure strategies for the transactional engine.

Three implementations of :class:`BackpressureStrategyPort`:

* :class:`AdaptiveBackpressureStrategy` -- dynamically adjusts concurrency
* :class:`BatchedBackpressureStrategy` -- fixed batch size processing
* :class:`CircuitBreakerBackpressureStrategy` -- opens circuit after threshold failures

Plus a :class:`BackpressureStrategyFactory` for creating strategies by name.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pyfly.transactional.shared.ports.outbound import BackpressureStrategyPort
from pyfly.transactional.shared.types import BackpressureConfig

logger = logging.getLogger(__name__)


class AdaptiveBackpressureStrategy:
    """Dynamically adjusts concurrency based on latency and error rates.

    Starts at the configured concurrency level and adapts:

    * **increases** concurrency when latency is low and no errors occur.
    * **decreases** concurrency on errors or high latency.

    Internally uses an :class:`asyncio.Semaphore` to cap parallelism.
    """

    @property
    def strategy_name(self) -> str:
        return "adaptive"

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
        config: BackpressureConfig,
    ) -> list[Any]:
        if not items:
            return []

        concurrency = config.concurrency
        semaphore = asyncio.Semaphore(concurrency)
        results: list[Any] = [None] * len(items)
        error_count = 0
        total_latency = 0.0
        completed = 0
        lock = asyncio.Lock()

        async def _process(index: int, item: Any) -> None:
            nonlocal error_count, total_latency, completed, semaphore, concurrency

            async with semaphore:
                start = time.monotonic()
                try:
                    results[index] = await processor(item)
                except Exception as exc:  # noqa: BLE001
                    results[index] = exc
                    async with lock:
                        error_count += 1
                finally:
                    elapsed = time.monotonic() - start
                    async with lock:
                        total_latency += elapsed
                        completed += 1

                        # Adapt concurrency every few completions
                        if completed % max(1, config.concurrency // 2) == 0:
                            avg_latency = total_latency / completed
                            if error_count > 0 or avg_latency > 1.0:
                                # Decrease concurrency (min 1)
                                concurrency = max(1, concurrency - 1)
                                semaphore = asyncio.Semaphore(concurrency)
                                logger.debug(
                                    "Adaptive: decreased concurrency to %d",
                                    concurrency,
                                )
                            elif avg_latency < 0.1 and error_count == 0:
                                # Increase concurrency
                                concurrency = concurrency + 1
                                semaphore = asyncio.Semaphore(concurrency)
                                logger.debug(
                                    "Adaptive: increased concurrency to %d",
                                    concurrency,
                                )

        tasks = [asyncio.create_task(_process(i, item)) for i, item in enumerate(items)]
        await asyncio.gather(*tasks)

        return results


class BatchedBackpressureStrategy:
    """Processes items in fixed-size batches.

    Each batch runs its items in parallel via :func:`asyncio.gather`.
    The strategy waits for the current batch to complete before starting the
    next one.
    """

    @property
    def strategy_name(self) -> str:
        return "batched"

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
        config: BackpressureConfig,
    ) -> list[Any]:
        if not items:
            return []

        batch_size = config.batch_size
        results: list[Any] = []

        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            batch_results = await asyncio.gather(
                *(processor(item) for item in batch),
                return_exceptions=True,
            )
            results.extend(batch_results)

        return results


class CircuitBreakerBackpressureStrategy:
    """Opens circuit after a threshold of consecutive failures.

    Tracks consecutive failures and transitions through three states:

    * **CLOSED** -- normal operation, items are processed.
    * **OPEN** -- circuit tripped; items are immediately rejected with an error.
    * **HALF_OPEN** -- after *wait_duration_ms*, a single probe is allowed
      through.  On success the circuit closes; on failure it re-opens.
    """

    @property
    def strategy_name(self) -> str:
        return "circuit_breaker"

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
        config: BackpressureConfig,
    ) -> list[Any]:
        if not items:
            return []

        consecutive_failures = 0
        circuit_open = False
        circuit_opened_at: float | None = None
        wait_duration_s = config.wait_duration_ms / 1000.0
        results: list[Any] = []

        for item in items:
            if circuit_open:
                # Check if we should transition to half-open
                if circuit_opened_at is not None and (time.monotonic() - circuit_opened_at) >= wait_duration_s:
                    # Half-open: try a single probe
                    try:
                        result = await processor(item)
                        # Success in half-open: close circuit
                        circuit_open = False
                        circuit_opened_at = None
                        consecutive_failures = 0
                        results.append(result)
                    except Exception as exc:  # noqa: BLE001
                        # Failed in half-open: re-open circuit
                        circuit_opened_at = time.monotonic()
                        results.append(exc)
                else:
                    # Circuit is open, reject immediately
                    results.append(RuntimeError(f"Circuit breaker is open — skipping item: {item}"))
            else:
                # Closed state: process normally
                try:
                    result = await processor(item)
                    consecutive_failures = 0
                    results.append(result)
                except Exception as exc:  # noqa: BLE001
                    consecutive_failures += 1
                    results.append(exc)

                    if consecutive_failures >= config.failure_threshold:
                        circuit_open = True
                        circuit_opened_at = time.monotonic()
                        logger.warning(
                            "Circuit breaker opened after %d consecutive failures",
                            consecutive_failures,
                        )

        return results


class BackpressureStrategyFactory:
    """Factory for creating backpressure strategies by type name."""

    @staticmethod
    def create(strategy_type: str) -> BackpressureStrategyPort:
        """Create a backpressure strategy by type name.

        Args:
            strategy_type: One of ``"adaptive"``, ``"batched"``,
                ``"circuit_breaker"``.

        Returns:
            A strategy instance satisfying :class:`BackpressureStrategyPort`.

        Raises:
            ValueError: If *strategy_type* is not recognised.
        """
        strategies: dict[str, type] = {
            "adaptive": AdaptiveBackpressureStrategy,
            "batched": BatchedBackpressureStrategy,
            "circuit_breaker": CircuitBreakerBackpressureStrategy,
        }

        strategy_cls = strategies.get(strategy_type)
        if strategy_cls is None:
            raise ValueError(
                f"Unknown backpressure strategy type: '{strategy_type}'. "
                f"Available types: {', '.join(sorted(strategies))}"
            )

        return cast(BackpressureStrategyPort, strategy_cls())
