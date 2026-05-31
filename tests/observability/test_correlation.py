# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the W3C correlation surface in :mod:`pyfly.observability.correlation`."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.observability.correlation import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TENANT_ID_HEADER,
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    bind_correlation_context,
    current_correlation_context,
    get_correlation_id,
    get_request_id,
    get_tenant_id,
    get_traceparent,
    get_tracestate,
    unbind_correlation_context,
)
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters.correlation_filter import CorrelationFilter

# ---------------------------------------------------------------------------
# correlation context vars
# ---------------------------------------------------------------------------


class TestCorrelationContextVars:
    def test_bind_and_read(self) -> None:
        tokens = bind_correlation_context(
            correlation_id="c1",
            request_id="r1",
            tenant_id="acme",
            traceparent="00-1-1-01",
            tracestate="vendor=x",
        )
        try:
            assert get_correlation_id() == "c1"
            assert get_request_id() == "r1"
            assert get_tenant_id() == "acme"
            assert get_traceparent() == "00-1-1-01"
            assert get_tracestate() == "vendor=x"
            assert current_correlation_context() == {
                CORRELATION_ID_HEADER: "c1",
                REQUEST_ID_HEADER: "r1",
                TENANT_ID_HEADER: "acme",
                TRACEPARENT_HEADER: "00-1-1-01",
                TRACESTATE_HEADER: "vendor=x",
            }
        finally:
            unbind_correlation_context(tokens)

        assert get_correlation_id() is None
        assert get_request_id() is None
        assert current_correlation_context() == {}

    def test_empty_values_omitted_from_context(self) -> None:
        tokens = bind_correlation_context(
            correlation_id="c1",
            request_id="r1",
            tenant_id=None,
            traceparent=None,
            tracestate=None,
        )
        try:
            ctx = current_correlation_context()
            assert ctx == {CORRELATION_ID_HEADER: "c1", REQUEST_ID_HEADER: "r1"}
        finally:
            unbind_correlation_context(tokens)

    @pytest.mark.asyncio
    async def test_propagation_through_gather(self) -> None:
        async def reader() -> str:
            return get_correlation_id() or "missing"

        tokens = bind_correlation_context(
            correlation_id="cid-xyz",
            request_id="rid-1",
            tenant_id=None,
            traceparent=None,
            tracestate=None,
        )
        try:
            results = await asyncio.gather(reader(), reader(), reader())
            assert results == ["cid-xyz", "cid-xyz", "cid-xyz"]
        finally:
            unbind_correlation_context(tokens)


# ---------------------------------------------------------------------------
# starlette CorrelationFilter middleware
# ---------------------------------------------------------------------------


def _build_app() -> Starlette:
    async def endpoint(request):
        return JSONResponse(
            {
                "correlation_id": get_correlation_id(),
                "request_id": get_request_id(),
                "tenant_id": get_tenant_id(),
                "traceparent": get_traceparent(),
                "tracestate": get_tracestate(),
            }
        )

    middleware = [Middleware(WebFilterChainMiddleware, filters=[CorrelationFilter()])]
    return Starlette(routes=[Route("/echo", endpoint)], middleware=middleware)


class TestCorrelationFilter:
    def test_generates_ids_when_absent(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo")
        assert resp.status_code == 200
        body = resp.json()
        # IDs were generated server-side
        uuid.UUID(body["correlation_id"])
        uuid.UUID(body["request_id"])
        # tenant + W3C absent
        assert body["tenant_id"] is None
        assert body["traceparent"] is None
        # response echoes generated IDs
        assert resp.headers[CORRELATION_ID_HEADER] == body["correlation_id"]
        assert resp.headers[REQUEST_ID_HEADER] == body["request_id"]
        assert TRACEPARENT_HEADER not in resp.headers
        assert TENANT_ID_HEADER not in resp.headers

    def test_echoes_inbound_headers(self) -> None:
        client = TestClient(_build_app())
        resp = client.get(
            "/echo",
            headers={
                CORRELATION_ID_HEADER: "fixed-c",
                REQUEST_ID_HEADER: "fixed-r",
                TENANT_ID_HEADER: "acme",
                TRACEPARENT_HEADER: "00-aabb-ccdd-01",
                TRACESTATE_HEADER: "vendor=acme",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["correlation_id"] == "fixed-c"
        assert body["request_id"] == "fixed-r"
        assert body["tenant_id"] == "acme"
        assert body["traceparent"] == "00-aabb-ccdd-01"
        assert body["tracestate"] == "vendor=acme"
        assert resp.headers[CORRELATION_ID_HEADER] == "fixed-c"
        assert resp.headers[TRACEPARENT_HEADER] == "00-aabb-ccdd-01"
        assert resp.headers[TENANT_ID_HEADER] == "acme"

    def test_context_cleared_after_request(self) -> None:
        client = TestClient(_build_app())
        client.get("/echo")
        # In TestClient each call is sync; the context should be unbound
        # after the response returns.
        assert get_correlation_id() is None
        assert get_request_id() is None
