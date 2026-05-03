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
"""Circuit breaker pattern for resilient service calls."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import Enum, auto
from typing import Any

from pyfly.kernel.exceptions import CircuitBreakerException


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """Circuit breaker that prevents cascading failures.

    Tracks consecutive failures and opens the circuit when the threshold
    is reached. After a recovery timeout, allows a single probe request
    (half-open state). If it succeeds, the circuit closes; if it fails,
    it re-opens.

    Args:
        failure_threshold: Number of consecutive failures before opening.
        recovery_timeout: How long to wait before allowing a probe request.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout.total_seconds()
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        """Current circuit state, accounting for recovery timeout."""
        if (
            self._state == CircuitState.OPEN
            and self._last_failure_time is not None
            and time.monotonic() - self._last_failure_time >= self._recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    async def call(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Execute a function through the circuit breaker."""
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitBreakerException("Circuit breaker is open")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except CircuitBreakerException:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """Reset state on successful call."""
        self._failure_count = 0
        self._state = CircuitState.CLOSED
        self._last_failure_time = None

    def _on_failure(self) -> None:
        """Record a failure and potentially open the circuit."""
        if self._state == CircuitState.HALF_OPEN:
            self._failure_count = self._failure_threshold
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            return
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
