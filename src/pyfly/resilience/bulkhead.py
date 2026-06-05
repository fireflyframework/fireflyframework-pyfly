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
"""Bulkhead pattern for concurrency isolation."""

from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Callable
from typing import Any

from pyfly.kernel.exceptions import BulkheadException


class Bulkhead:
    """Limits concurrent execution of a resource.

    A single lock-guarded permit counter is the sole source of truth, shared by
    both sync and async decorated calls, so the accounting can never desynchronise
    when one Bulkhead instance decorates both (audit #189). When max_concurrent
    calls are already in-flight, new calls raise BulkheadException immediately
    (no waiting/queueing), matching Resilience4j's zero-wait semaphore bulkhead.

    Args:
        max_concurrent: Maximum number of concurrent calls allowed.
    """

    def __init__(self, max_concurrent: int = 10) -> None:
        self._max_concurrent = max_concurrent
        self._active = 0
        self._lock = threading.Lock()

    def _acquire_slot(self) -> None:
        """Atomically reserve a permit or raise if at capacity."""
        with self._lock:
            if self._active >= self._max_concurrent:
                raise BulkheadException(f"Bulkhead at capacity ({self._max_concurrent} concurrent calls)")
            self._active += 1

    def _release_slot(self) -> None:
        """Atomically return a permit."""
        with self._lock:
            if self._active > 0:
                self._active -= 1

    async def acquire(self) -> None:
        """Try to acquire a slot. Raises BulkheadException if at capacity."""
        self._acquire_slot()

    def release(self) -> None:
        """Release a slot."""
        self._release_slot()

    @property
    def available_slots(self) -> int:
        """Number of available concurrent slots."""
        return self._max_concurrent - self._active

    @property
    def max_concurrent(self) -> int:
        """Maximum concurrent calls allowed."""
        return self._max_concurrent


def bulkhead(
    bh: Bulkhead,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that applies bulkhead concurrency limiting.

    Args:
        bh: The Bulkhead instance to use.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Same permit primitive as the async path so a Bulkhead shared
                # across sync + async never desynchronises (audit #189).
                bh._acquire_slot()
                try:
                    return func(*args, **kwargs)
                finally:
                    bh._release_slot()

            return sync_wrapper

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await bh.acquire()
            try:
                return await func(*args, **kwargs)
            finally:
                bh.release()

        return wrapper

    return decorator
