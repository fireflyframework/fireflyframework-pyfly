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
"""TracingFilter — extracts the inbound W3C trace context and opens a SERVER span.

So every span created during the request (e.g. via ``@span``) and every log line is part
of the upstream distributed trace. No-op when opentelemetry is not installed.
"""

from __future__ import annotations

from typing import cast

from starlette.requests import Request
from starlette.responses import Response

from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.observability.propagation import extract_context, has_otel
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - exercised without the observability extra
    trace = None  # type: ignore[assignment]


@order(HIGHEST_PRECEDENCE + 60)  # just after CorrelationFilter (+50)
class TracingFilter(OncePerRequestFilter):
    """Opens a server span as a child of the extracted upstream trace context."""

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        if not has_otel():
            return cast(Response, await call_next(request))
        parent = extract_context(request.headers)
        tracer = trace.get_tracer("pyfly")
        with tracer.start_as_current_span(
            f"{request.method} {request.url.path}",
            context=parent,
            kind=trace.SpanKind.SERVER,
        ) as span:
            response = cast(Response, await call_next(request))
            try:
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("url.path", request.url.path)
                span.set_attribute("http.response.status_code", response.status_code)
            except Exception:  # noqa: BLE001 - attribute tagging must never break the response
                pass
            return response
