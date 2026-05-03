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
"""Fallback decorator for graceful degradation."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any


def fallback(
    *,
    fallback_method: Callable[..., Any] | None = None,
    fallback_value: Any = None,
    on: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for fallback behavior on failure.

    Exactly one of fallback_method or fallback_value should be provided.
    When fallback_method is used, it receives the same arguments as the
    original function plus the exception as a keyword argument ``exc``.

    Args:
        fallback_method: Callable to invoke on failure. Receives same args + exc kwarg.
        fallback_value: Static value to return on failure.
        on: Exception types to catch (default: all exceptions).
    """
    if fallback_method is None and fallback_value is None:
        raise ValueError("Either fallback_method or fallback_value must be provided")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return func(*args, **kwargs)
                except on as exc:
                    if fallback_method is not None:
                        result = fallback_method(*args, exc=exc, **kwargs)
                        return result
                    return fallback_value

            return sync_wrapper

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except on as exc:
                if fallback_method is not None:
                    try:
                        result = fallback_method(*args, exc=exc, **kwargs)
                    except TypeError as te:
                        if "exc" in str(te):
                            raise TypeError(
                                f"Fallback method '{fallback_method.__name__}' must accept an 'exc' keyword argument"
                            ) from te
                        raise
                    if inspect.isawaitable(result):
                        return await result
                    return result
                return fallback_value

        return wrapper

    return decorator
