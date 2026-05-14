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
"""Cross-cutting correlation context for distributed tracing.

This module exposes the W3C Trace Context surface plus Firefly's
service-correlation headers as a set of :mod:`contextvars` that
propagate automatically through ``await`` chains and ``asyncio.gather``
fan-outs. Web filters stamp the values on inbound requests; service
code reads them via :func:`current_correlation_context` (or the
individual getters) without needing the originating ``Request``
threaded through every signature.

Headers covered:

* ``X-Correlation-Id`` — end-to-end correlation across service hops.
  Echoed verbatim when supplied; generated as a UUID when absent.
* ``X-Request-Id`` — one identifier per HTTP call. Generated when
  absent.
* ``X-Tenant-Id`` — multi-tenant scope. Never generated server-side;
  absent means "unscoped".
* ``traceparent`` and ``tracestate`` — W3C Trace Context for
  OpenTelemetry. Echoed unchanged when present so downstream services
  receive an unbroken chain.

Pyfly's :class:`pyfly.web.adapters.starlette.filters.correlation_filter.CorrelationFilter`
is the producer; this module is the consumer-facing API.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

CORRELATION_ID_HEADER = "X-Correlation-Id"
REQUEST_ID_HEADER = "X-Request-Id"
TENANT_ID_HEADER = "X-Tenant-Id"
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

_correlation_id: ContextVar[str | None] = ContextVar(
    "pyfly_correlation_id", default=None
)
_request_id: ContextVar[str | None] = ContextVar(
    "pyfly_request_id", default=None
)
_tenant_id: ContextVar[str | None] = ContextVar(
    "pyfly_tenant_id", default=None
)
_traceparent: ContextVar[str | None] = ContextVar(
    "pyfly_traceparent", default=None
)
_tracestate: ContextVar[str | None] = ContextVar(
    "pyfly_tracestate", default=None
)


def set_correlation_id(value: str | None) -> Token[str | None]:
    return _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


def set_request_id(value: str | None) -> Token[str | None]:
    return _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_tenant_id(value: str | None) -> Token[str | None]:
    return _tenant_id.set(value)


def get_tenant_id() -> str | None:
    return _tenant_id.get()


def set_traceparent(value: str | None) -> Token[str | None]:
    return _traceparent.set(value)


def get_traceparent() -> str | None:
    return _traceparent.get()


def set_tracestate(value: str | None) -> Token[str | None]:
    return _tracestate.set(value)


def get_tracestate() -> str | None:
    return _tracestate.get()


def current_correlation_context() -> dict[str, str]:
    """Return the active correlation surface as a header dict.

    Empty values are omitted so the dict can be merged straight into
    outbound requests.
    """
    ctx: dict[str, str] = {}
    cid = _correlation_id.get()
    if cid:
        ctx[CORRELATION_ID_HEADER] = cid
    rid = _request_id.get()
    if rid:
        ctx[REQUEST_ID_HEADER] = rid
    tid = _tenant_id.get()
    if tid:
        ctx[TENANT_ID_HEADER] = tid
    tp = _traceparent.get()
    if tp:
        ctx[TRACEPARENT_HEADER] = tp
    ts = _tracestate.get()
    if ts:
        ctx[TRACESTATE_HEADER] = ts
    return ctx


class CorrelationContextTokens:
    """Container for ``ContextVar`` reset tokens covering the full surface.

    Used by the web filter to reset all vars in one ``finally`` block.
    """

    __slots__ = ("correlation_id", "request_id", "tenant_id", "traceparent", "tracestate")

    def __init__(
        self,
        correlation_id: Token[str | None],
        request_id: Token[str | None],
        tenant_id: Token[str | None],
        traceparent: Token[str | None],
        tracestate: Token[str | None],
    ) -> None:
        self.correlation_id = correlation_id
        self.request_id = request_id
        self.tenant_id = tenant_id
        self.traceparent = traceparent
        self.tracestate = tracestate


def bind_correlation_context(
    *,
    correlation_id: str | None,
    request_id: str | None,
    tenant_id: str | None,
    traceparent: str | None,
    tracestate: str | None,
) -> CorrelationContextTokens:
    """Bind all five context vars at once and return tokens for reset."""
    return CorrelationContextTokens(
        correlation_id=_correlation_id.set(correlation_id),
        request_id=_request_id.set(request_id),
        tenant_id=_tenant_id.set(tenant_id),
        traceparent=_traceparent.set(traceparent),
        tracestate=_tracestate.set(tracestate),
    )


def unbind_correlation_context(tokens: CorrelationContextTokens) -> None:
    """Reset every context var bound by :func:`bind_correlation_context`."""
    _correlation_id.reset(tokens.correlation_id)
    _request_id.reset(tokens.request_id)
    _tenant_id.reset(tokens.tenant_id)
    _traceparent.reset(tokens.traceparent)
    _tracestate.reset(tokens.tracestate)
