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
"""Retry with exponential backoff for transient failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any


class RetryPolicy:
    """Retry policy with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including the first).
        base_delay: Base delay between retries (doubled each attempt).
        retry_on: Tuple of exception types to retry on. Defaults to all.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: timedelta = timedelta(seconds=1),
        retry_on: tuple[type[Exception], ...] = (Exception,),
    ) -> None:
        self._max_attempts = max_attempts
        self._base_delay = base_delay.total_seconds()
        self._retry_on = retry_on

    async def execute(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Execute a function with retry logic."""
        last_exception: Exception | None = None

        for attempt in range(self._max_attempts):
            try:
                return await func(*args, **kwargs)
            except self._retry_on as exc:
                last_exception = exc
                if attempt < self._max_attempts - 1:
                    delay = self._base_delay * (2**attempt)
                    await asyncio.sleep(delay)
            except Exception:
                raise

        raise last_exception  # type: ignore[misc]
