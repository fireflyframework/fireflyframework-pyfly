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
"""Controller-level exception handler decorator."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def exception_handler(exc_type: type[Exception]) -> Callable[[F], F]:
    """Mark a controller method as an exception handler.

    The handler is called when the specified exception type is raised
    by any handler in the same controller. Returns ``(status_code, body)``
    tuple or a Starlette Response.

    Usage::

        @exception_handler(OrderNotFoundException)
        async def handle_not_found(self, exc):
            return 404, {"error": "not found"}
    """

    def decorator(func: F) -> F:
        func.__pyfly_exception_handler__ = exc_type  # type: ignore[attr-defined]
        return func

    return decorator
