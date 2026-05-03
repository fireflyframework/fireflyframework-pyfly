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
"""WebFilter protocol — framework-agnostic filter interface.

Uses generic ``Any`` types for Request/Response so that vendor-specific
types (e.g. Starlette) remain confined to the adapter layer.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Protocol, runtime_checkable

# Type alias for the next callable in the filter chain.
# Concrete type: Callable[[Request], Coroutine[Any, Any, Response]]
CallNext = Callable[..., Coroutine[Any, Any, Any]]


@runtime_checkable
class WebFilter(Protocol):
    """Protocol for HTTP request/response filters.

    Filters are executed in order (sorted by ``@order``) inside a single
    ``WebFilterChainMiddleware``.  Each filter can inspect/modify the request,
    delegate to ``call_next``, and inspect/modify the response.

    Implement this protocol directly *or* extend ``OncePerRequestFilter``
    for automatic URL-pattern matching.
    """

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        """Execute this filter's logic.

        Args:
            request: The incoming HTTP request.
            call_next: Calls the next filter in the chain (or the route handler).

        Returns:
            The HTTP response.
        """
        ...

    def should_not_filter(self, request: Any) -> bool:
        """Return ``True`` to skip this filter for the given request."""
        ...
