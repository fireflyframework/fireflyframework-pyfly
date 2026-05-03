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
"""Rate limiter using token bucket algorithm."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from pyfly.kernel.exceptions import RateLimitException


class RateLimiter:
    """Token bucket rate limiter.

    Tokens are added at a fixed rate up to a maximum capacity. Each call
    consumes one token. When no tokens are available, RateLimitException is raised.

    Args:
        max_tokens: Maximum bucket capacity (burst size).
        refill_rate: Tokens added per second.
    """

    def __init__(self, max_tokens: int = 10, refill_rate: float = 10.0) -> None:
        self._max_tokens = max_tokens
        self._refill_rate = refill_rate
        self._tokens = float(max_tokens)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a token. Raises RateLimitException if none available."""
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                raise RateLimitException("Rate limit exceeded")
            self._tokens -= 1.0

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        """Current number of available tokens (computed without mutating state)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(self._max_tokens, self._tokens + elapsed * self._refill_rate)


def rate_limiter(
    limiter: RateLimiter,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that applies rate limiting to an async function.

    Args:
        limiter: The RateLimiter instance to use.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                limiter._refill()
                if limiter._tokens < 1.0:
                    raise RateLimitException("Rate limit exceeded")
                limiter._tokens -= 1.0
                return func(*args, **kwargs)

            return sync_wrapper

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await limiter.acquire()
            return await func(*args, **kwargs)

        return wrapper

    return decorator
