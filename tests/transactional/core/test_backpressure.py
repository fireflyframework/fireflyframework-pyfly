# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for BackpressureStrategy implementations."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.backpressure import (
    AdaptiveBackpressureStrategy,
    BatchedBackpressureStrategy,
    CircuitBreakerBackpressureStrategy,
    make_strategy,
)
from pyfly.transactional.core.model import BackpressureProperties


@pytest.mark.asyncio
async def test_adaptive_runs_all() -> None:
    strategy = AdaptiveBackpressureStrategy(concurrency=2)

    async def double(x: int) -> int:
        return x * 2

    result = await strategy.apply([1, 2, 3], double)
    assert result == [2, 4, 6]


@pytest.mark.asyncio
async def test_batched_runs_in_chunks() -> None:
    strategy = BatchedBackpressureStrategy(batch_size=2)
    seen: list[int] = []

    async def collect(x: int) -> int:
        seen.append(x)
        return x

    result = await strategy.apply([1, 2, 3, 4, 5], collect)
    assert sorted(result) == [1, 2, 3, 4, 5]
    assert sorted(seen) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_threshold() -> None:
    strategy = CircuitBreakerBackpressureStrategy()

    async def boom(_: int) -> int:
        msg = "fail"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        await strategy.apply([1, 2, 3, 4, 5, 6, 7], boom)


def test_make_strategy_resolves_by_name() -> None:
    s = make_strategy(BackpressureProperties(strategy="adaptive"))
    assert s.name == "adaptive"
    s = make_strategy(BackpressureProperties(strategy="batched"))
    assert s.name == "batched"
    s = make_strategy(BackpressureProperties(strategy="circuit-breaker"))
    assert s.name == "circuit-breaker"
