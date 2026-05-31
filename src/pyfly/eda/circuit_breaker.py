# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Circuit breaker for event handlers — fast-fails when handlers are unhealthy."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_ms: int = 30_000
    half_open_max_calls: int = 3


class CircuitOpenError(RuntimeError):
    pass


class EventCircuitBreaker:
    """Wrap an event handler in a Resilience4j-style circuit breaker."""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    async def execute(self, awaitable_factory: object) -> object:
        await self._before_call()
        try:
            result = await awaitable_factory()  # type: ignore[operator]
            await self._on_success()
            return result
        except Exception:
            await self._on_failure()
            raise

    async def _before_call(self) -> None:
        async with self._lock:
            if self._opened_at is None:
                return
            elapsed_ms = (time.monotonic() - self._opened_at) * 1000.0
            if elapsed_ms < self._config.recovery_timeout_ms:
                msg = "circuit breaker OPEN"
                raise CircuitOpenError(msg)
            if self._half_open_calls >= self._config.half_open_max_calls:
                msg = "circuit breaker half-open quota exhausted"
                raise CircuitOpenError(msg)
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
                # Reset the half-open trial budget so the NEXT recovery window
                # gets a fresh quota. Without this the counter accumulates across
                # re-opens and the breaker becomes permanently stuck OPEN once
                # half_open_max_calls total trials have failed over its lifetime.
                self._half_open_calls = 0
