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
"""@retry — declarative retry with backoff (Spring Retry / Resilience4j @Retry equivalent)."""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import Callable
from typing import Any


def retry(
    max_attempts: int = 3,
    *,
    delay: float = 0.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Re-invoke the wrapped callable up to *max_attempts* times while it raises one of
    *exceptions*, sleeping ``delay * backoff ** attempt`` (capped at *max_delay*) between
    attempts. The last exception is re-raised once attempts are exhausted. Works on both
    sync and async callables.

    Args:
        max_attempts: Total attempts including the first (>= 1).
        delay: Base delay (seconds) before the first retry.
        backoff: Multiplier applied to the delay each subsequent attempt.
        max_delay: Optional cap on the per-attempt delay.
        exceptions: Exception types that trigger a retry; others propagate immediately.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def _wait(attempt: int) -> float:
        computed = delay * (backoff**attempt)
        return min(computed, max_delay) if max_delay is not None else computed

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                last: BaseException | None = None
                for attempt in range(max_attempts):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as exc:
                        last = exc
                        if attempt + 1 >= max_attempts:
                            break
                        wait = _wait(attempt)
                        if wait > 0:
                            await asyncio.sleep(wait)
                assert last is not None  # noqa: S101 - loop always sets last before break
                raise last

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            last: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last = exc
                    if attempt + 1 >= max_attempts:
                        break
                    wait = _wait(attempt)
                    if wait > 0:
                        time.sleep(wait)
            assert last is not None  # noqa: S101 - loop always sets last before break
            raise last

        return sync_wrapper

    return decorator
