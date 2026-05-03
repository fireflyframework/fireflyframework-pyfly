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
"""SSE mapping decorator for class-based controllers.

Mirrors the ``@websocket_mapping`` pattern from
:mod:`pyfly.websocket.decorators`, but marks a method as a Server-Sent
Events endpoint instead of a WebSocket handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def sse_mapping(path: str = "") -> Callable[[F], F]:
    """Mark a controller method as a Server-Sent Events endpoint.

    The decorated method should be an async generator that yields data
    objects.  Each yielded value is automatically formatted as an SSE
    event and streamed to the client.

    Usage::

        @rest_controller
        @request_mapping("/events")
        class StockController:

            @sse_mapping("/prices")
            async def stream_prices(self):
                while True:
                    yield {"symbol": "AAPL", "price": 150.25}
                    await asyncio.sleep(1)
    """

    def decorator(func: F) -> F:
        func.__pyfly_sse_mapping__ = {"path": path}  # type: ignore[attr-defined]
        return func

    return decorator
