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
"""W3C Trace Context + service correlation filter.

Stamps every request/response with the full correlation surface:

* ``X-Correlation-Id`` — service-hop correlation. Echoed when supplied,
  generated as a UUID when absent.
* ``X-Request-Id`` — one identifier per HTTP call. Generated when absent.
* ``X-Tenant-Id`` — multi-tenant scope. Never generated server-side.
* ``traceparent`` / ``tracestate`` — W3C Trace Context for OpenTelemetry.
  Echoed unchanged when present.

Each value is also bound to a :mod:`contextvars` so deeply nested
service code can read it via
:func:`pyfly.observability.correlation.current_correlation_context`
without taking the originating ``Request`` in its constructor.
"""

from __future__ import annotations

import uuid
from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.observability.correlation import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    bind_correlation_context,
    unbind_correlation_context,
)
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext


@order(HIGHEST_PRECEDENCE + 50)
class CorrelationFilter(OncePerRequestFilter):
    """Stamp the correlation/W3C tracing surface on every request/response.

    Runs ahead of :class:`pyfly.web.adapters.starlette.filters.transaction_id_filter.TransactionIdFilter`
    so the transaction ID can be enriched with the correlation context
    when it is generated.
    """

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        headers = request.headers
        correlation_id = headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
        request_id = headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        tenant_id = headers.get(TENANT_ID_HEADER) or ""
        traceparent = headers.get(TRACEPARENT_HEADER) or ""
        tracestate = headers.get(TRACESTATE_HEADER) or ""

        request.state.correlation_id = correlation_id
        request.state.request_id_header = request_id
        request.state.tenant_id = tenant_id
        request.state.traceparent = traceparent
        request.state.tracestate = tracestate

        tokens = bind_correlation_context(
            correlation_id=correlation_id,
            request_id=request_id,
            tenant_id=tenant_id or None,
            traceparent=traceparent or None,
            tracestate=tracestate or None,
        )

        try:
            response: Response = cast(Response, await call_next(request))
        finally:
            unbind_correlation_context(tokens)

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        response.headers[REQUEST_ID_HEADER] = request_id
        if tenant_id:
            response.headers[TENANT_ID_HEADER] = tenant_id
        if traceparent:
            response.headers[TRACEPARENT_HEADER] = traceparent
        if tracestate:
            response.headers[TRACESTATE_HEADER] = tracestate
        return response
