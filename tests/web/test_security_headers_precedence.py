# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
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
"""Security defaults preserve explicit application response policies."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import SecurityHeadersFilter
from pyfly.web.adapters.starlette.security_headers import SecurityHeadersMiddleware
from pyfly.web.security_headers import SecurityHeadersConfig


@pytest.mark.parametrize("adapter", ["filter", "middleware"])
@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "SAMEORIGIN"),
        ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
        ("X-XSS-Protection", "1; mode=block"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", "default-src 'none'"),
        ("Permissions-Policy", "camera=(), microphone=()"),
    ],
)
def test_explicit_response_policy_is_preserved_once(adapter: str, header: str, value: str) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        response = PlainTextResponse("private evidence", headers={header: value, "Cache-Control": "private, no-store"})
        response.set_cookie("first", "one")
        response.set_cookie("second", "two")
        return response

    config = SecurityHeadersConfig(content_security_policy="default-src 'self'", permissions_policy="geolocation=()")
    middleware = (
        Middleware(WebFilterChainMiddleware, filters=[SecurityHeadersFilter(config)])
        if adapter == "filter"
        else Middleware(SecurityHeadersMiddleware, config=config)
    )
    app = Starlette(routes=[Route("/evidence", endpoint)], middleware=[middleware])
    response = TestClient(app).get("/evidence")

    assert response.status_code == 200
    assert response.headers.get_list(header) == [value]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers.get_list("Set-Cookie") == [
        "first=one; Path=/; SameSite=lax",
        "second=two; Path=/; SameSite=lax",
    ]
    if header != "Referrer-Policy":
        assert response.headers.get_list("Referrer-Policy") == ["strict-origin-when-cross-origin"]
    if header != "Content-Security-Policy":
        assert response.headers.get_list("Content-Security-Policy") == ["default-src 'self'"]


@pytest.mark.parametrize("adapter", ["filter", "middleware"])
def test_existing_policy_name_is_matched_case_insensitively(adapter: str) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        response = PlainTextResponse("private evidence")
        response.raw_headers.append((b"ReFeRrEr-PoLiCy", b"no-referrer"))
        return response

    middleware = (
        Middleware(WebFilterChainMiddleware, filters=[SecurityHeadersFilter()])
        if adapter == "filter"
        else Middleware(SecurityHeadersMiddleware)
    )
    app = Starlette(routes=[Route("/evidence", endpoint)], middleware=[middleware])
    response = TestClient(app).get("/evidence")

    assert response.headers.get_list("Referrer-Policy") == ["no-referrer"]
    assert response.headers.get_list("X-Content-Type-Options") == ["nosniff"]


def test_composed_public_adapters_do_not_duplicate_security_defaults() -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("private evidence", headers={"Referrer-Policy": "no-referrer"})

    app = Starlette(
        routes=[Route("/evidence", endpoint)],
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            Middleware(WebFilterChainMiddleware, filters=[SecurityHeadersFilter()]),
        ],
    )
    response = TestClient(app).get("/evidence")

    assert response.headers.get_list("X-Content-Type-Options") == ["nosniff"]
    assert response.headers.get_list("Referrer-Policy") == ["no-referrer"]
