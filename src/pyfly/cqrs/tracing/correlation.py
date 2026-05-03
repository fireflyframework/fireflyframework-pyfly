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
"""Correlation context for distributed tracing using contextvars.

Provides the Python equivalent of Java's ThreadLocal-based CorrelationContext,
using :mod:`contextvars` which works correctly with asyncio.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import uuid4

_correlation_id: ContextVar[str | None] = ContextVar("cqrs_correlation_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("cqrs_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("cqrs_span_id", default=None)

_logger = logging.getLogger(__name__)

HEADER_CORRELATION_ID = "X-Correlation-ID"
HEADER_TRACE_ID = "X-Trace-ID"
HEADER_SPAN_ID = "X-Span-ID"


class CorrelationContext:
    """Manages correlation IDs for distributed tracing across async boundaries.

    All methods are static — the state lives in :mod:`contextvars` and propagates
    automatically through ``await`` chains.
    """

    # ── correlation id ─────────────────────────────────────────

    @staticmethod
    def generate_correlation_id() -> str:
        return str(uuid4())

    @staticmethod
    def set_correlation_id(correlation_id: str) -> None:
        _correlation_id.set(correlation_id)
        _logger.debug("Correlation ID set: %s", correlation_id)

    @staticmethod
    def get_correlation_id() -> str | None:
        return _correlation_id.get()

    @staticmethod
    def get_or_create_correlation_id() -> str:
        cid = _correlation_id.get()
        if cid is None:
            cid = str(uuid4())
            _correlation_id.set(cid)
        return cid

    # ── trace / span ───────────────────────────────────────────

    @staticmethod
    def set_trace_id(trace_id: str) -> None:
        _trace_id.set(trace_id)

    @staticmethod
    def get_trace_id() -> str | None:
        return _trace_id.get()

    @staticmethod
    def set_span_id(span_id: str) -> None:
        _span_id.set(span_id)

    @staticmethod
    def get_span_id() -> str | None:
        return _span_id.get()

    # ── header propagation ─────────────────────────────────────

    @staticmethod
    def create_context_headers() -> dict[str, str]:
        """Build headers dict suitable for outbound HTTP or message calls."""
        headers: dict[str, str] = {}
        cid = _correlation_id.get()
        if cid:
            headers[HEADER_CORRELATION_ID] = cid
        tid = _trace_id.get()
        if tid:
            headers[HEADER_TRACE_ID] = tid
        sid = _span_id.get()
        if sid:
            headers[HEADER_SPAN_ID] = sid
        return headers

    @staticmethod
    def extract_context_from_headers(headers: dict[str, str]) -> None:
        """Restore context from inbound headers."""
        cid = headers.get(HEADER_CORRELATION_ID)
        if cid:
            _correlation_id.set(cid)
        tid = headers.get(HEADER_TRACE_ID)
        if tid:
            _trace_id.set(tid)
        sid = headers.get(HEADER_SPAN_ID)
        if sid:
            _span_id.set(sid)

    # ── lifecycle ──────────────────────────────────────────────

    @staticmethod
    def clear() -> None:
        """Clear all context vars — call after completing a request/message."""
        _correlation_id.set(None)
        _trace_id.set(None)
        _span_id.set(None)
