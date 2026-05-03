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
"""RequestContextFilter — initializes RequestContext for each HTTP request.

Runs at highest priority so all downstream filters and handlers
can access the request context.
"""

from __future__ import annotations

from typing import Any

from pyfly.container.ordering import HIGHEST_PRECEDENCE
from pyfly.context.request_context import RequestContext
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


class RequestContextFilter(OncePerRequestFilter):
    """Creates a fresh RequestContext for each incoming HTTP request.

    Honors the ``X-Request-Id`` header if present; otherwise generates a UUID.
    Clears the context after the response is sent (even on error).
    """

    __pyfly_order__ = HIGHEST_PRECEDENCE

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        request_id = getattr(request, "headers", {}).get("x-request-id")
        RequestContext.init(request_id=request_id)
        try:
            response = await call_next(request)
            return response
        finally:
            RequestContext.clear()
