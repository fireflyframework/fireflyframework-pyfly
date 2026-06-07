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
"""W3C trace-context propagation (OpenTelemetry).

Extracts the upstream trace context from inbound request headers and injects the
current context into outbound request headers, so a distributed trace flows across
service boundaries. Every function is a safe no-op when ``opentelemetry`` is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from opentelemetry import propagate, trace

    _HAS_OTEL = True
except ImportError:  # pragma: no cover - exercised without the observability extra
    _HAS_OTEL = False
    propagate = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]


def has_otel() -> bool:
    """Whether OpenTelemetry is installed."""
    return _HAS_OTEL


def extract_context(headers: Mapping[str, str]) -> Any:
    """Extract the upstream trace context from inbound *headers* (``None`` without OTel).

    The result is passed as ``context=`` to ``start_as_current_span`` so the server span
    becomes a child of the caller's trace.
    """
    if not _HAS_OTEL:
        return None
    return propagate.extract(dict(headers))


def inject_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject the current trace context into *headers* in place (W3C ``traceparent`` etc.)."""
    if _HAS_OTEL:
        propagate.inject(headers)
    return headers


def current_trace_ids() -> tuple[str, str] | None:
    """``(trace_id, span_id)`` as hex for the active span, or ``None`` if none / no OTel."""
    if not _HAS_OTEL:
        return None
    span_context = trace.get_current_span().get_span_context()
    if span_context is None or not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")
