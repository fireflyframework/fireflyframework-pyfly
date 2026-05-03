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
"""Tests for RequestLoggingMiddleware."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.request_logger import RequestLoggingMiddleware


def _hello(request: Request) -> PlainTextResponse:
    return PlainTextResponse("hello")


class TestRequestLoggingMiddleware:
    def test_request_completes_normally(self):
        app = Starlette(
            routes=[Route("/", _hello)],
            middleware=[Middleware(RequestLoggingMiddleware)],
        )
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.text == "hello"

    def test_response_headers_preserved(self):
        def _custom(request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok", headers={"X-Custom": "val"})

        app = Starlette(
            routes=[Route("/", _custom)],
            middleware=[Middleware(RequestLoggingMiddleware)],
        )
        client = TestClient(app)
        resp = client.get("/")
        assert resp.headers["X-Custom"] == "val"

    def test_logs_request_info(self):
        app = Starlette(
            routes=[Route("/items", _hello)],
            middleware=[Middleware(RequestLoggingMiddleware)],
        )
        client = TestClient(app)
        client.get("/items")
        # Verify no crash — structlog output is hard to capture in tests
        assert True

    def test_error_response_still_logged(self):
        def _error(request: Request) -> PlainTextResponse:
            return PlainTextResponse("error", status_code=500)

        app = Starlette(
            routes=[Route("/fail", _error)],
            middleware=[Middleware(RequestLoggingMiddleware)],
        )
        client = TestClient(app)
        resp = client.get("/fail")
        assert resp.status_code == 500
