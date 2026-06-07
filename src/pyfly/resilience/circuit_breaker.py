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
"""@circuit_breaker — closed/open/half-open state machine (Resilience4j equivalent)."""

from __future__ import annotations

import functools
import inspect
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from pyfly.kernel.exceptions import CircuitBreakerException


class CircuitState(Enum):
    """Circuit breaker state."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """A thread-safe circuit breaker.

    Trips OPEN after *failure_threshold* consecutive failures; after
    *recovery_timeout* seconds it moves to HALF_OPEN and allows a trial call —
    success closes the circuit, failure re-opens it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected = expected
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state is CircuitState.OPEN and (time.monotonic() - self._opened_at) >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN

    def before_call(self) -> None:
        """Raise :class:`CircuitBreakerException` when the circuit is open."""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                raise CircuitBreakerException("Circuit breaker is open")

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()


def circuit_breaker(breaker: CircuitBreaker) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Guard a callable with *breaker*: rejects calls while OPEN, records
    success/failure otherwise. Only ``breaker.expected`` exceptions trip it."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                breaker.before_call()
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    if isinstance(exc, breaker.expected):
                        breaker.on_failure()
                    raise
                breaker.on_success()
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            breaker.before_call()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, breaker.expected):
                    breaker.on_failure()
                raise
            breaker.on_success()
            return result

        return sync_wrapper

    return decorator
