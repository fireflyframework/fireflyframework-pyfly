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
"""PyFly SSE — decorator-driven Server-Sent Events support for controllers.

Usage::

    from pyfly.web.sse import SseEmitter, format_sse_event, sse_mapping

    @rest_controller
    @request_mapping("/events")
    class StockController:

        @sse_mapping("/prices")
        async def stream_prices(self):
            while True:
                yield {"symbol": "AAPL", "price": 150.25}
                await asyncio.sleep(1)
"""

from pyfly.web.sse.decorators import sse_mapping
from pyfly.web.sse.response import SseEmitter, format_sse_event

__all__ = [
    "SseEmitter",
    "format_sse_event",
    "sse_mapping",
]
