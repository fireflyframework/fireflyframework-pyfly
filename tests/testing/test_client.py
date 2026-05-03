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
"""Tests for PyFlyTestClient — fluent HTTP test assertions."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.testing.client import PyFlyTestClient


async def hello(request):
    return JSONResponse(
        {"message": "hello", "user": {"name": "Alice", "id": 1}},
        headers={"X-Custom": "test-value"},
    )


async def not_found(request):
    return JSONResponse({"error": "not found"}, status_code=404)


app = Starlette(
    routes=[
        Route("/api/hello", hello),
        Route("/api/missing", not_found),
    ]
)


@pytest.fixture
def client():
    return PyFlyTestClient(app)


class TestAssertStatus:
    def test_assert_status_passes(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_status(200)

    def test_assert_status_fails(self, client: PyFlyTestClient) -> None:
        with pytest.raises(AssertionError, match="Expected status 200"):
            client.get("/api/missing").assert_status(200)


class TestAssertJsonPath:
    def test_json_path_value(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_json_path("message", value="hello")

    def test_nested_json_path(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_json_path("user.name", value="Alice")

    def test_json_path_exists(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_json_path("user.id", exists=True)

    def test_json_path_not_exists(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_json_path("nonexistent", exists=False)


class TestAssertHeader:
    def test_header_exists(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_header("X-Custom", exists=True)

    def test_header_value(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_header("X-Custom", value="test-value")

    def test_header_not_exists(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_header("X-Missing", exists=False)


class TestAssertBodyContains:
    def test_body_contains(self, client: PyFlyTestClient) -> None:
        client.get("/api/hello").assert_body_contains("Alice")


class TestChaining:
    def test_fluent_chaining(self, client: PyFlyTestClient) -> None:
        (
            client.get("/api/hello")
            .assert_status(200)
            .assert_json_path("message", value="hello")
            .assert_header("X-Custom", exists=True)
            .assert_body_contains("Alice")
        )


class TestHttpMethods:
    def test_post(self, client: PyFlyTestClient) -> None:
        client.post("/api/hello").assert_status(405)

    def test_put(self, client: PyFlyTestClient) -> None:
        client.put("/api/hello").assert_status(405)

    def test_delete(self, client: PyFlyTestClient) -> None:
        client.delete("/api/hello").assert_status(405)

    def test_patch(self, client: PyFlyTestClient) -> None:
        client.patch("/api/hello").assert_status(405)
