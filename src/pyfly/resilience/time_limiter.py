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
"""Time limiter decorator for operation timeout control."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from pyfly.kernel.exceptions import OperationTimeoutException


def time_limiter(
    timeout: timedelta,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that limits execution time of an async function.

    Args:
        timeout: Maximum time allowed for the function to complete.

    Raises:
        OperationTimeoutException: If the function exceeds the timeout.
    """
    timeout_seconds = timeout.total_seconds()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Run in a worker thread and wait with a fractional timeout.
                # SIGALRM (the old approach) truncated sub-second timeouts to
                # whole seconds and crashed off the main thread (audit #184).
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(func, *args, **kwargs)
                    try:
                        return future.result(timeout=timeout_seconds)
                    except concurrent.futures.TimeoutError as exc:
                        raise OperationTimeoutException(
                            f"{func.__name__} exceeded timeout of {timeout_seconds}s"
                        ) from exc

            return sync_wrapper

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)
            except TimeoutError as exc:
                raise OperationTimeoutException(f"{func.__name__} exceeded timeout of {timeout_seconds}s") from exc

        return wrapper

    return decorator
