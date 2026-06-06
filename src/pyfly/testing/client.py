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
"""PyFlyTestClient — HTTP test client with fluent assertion methods."""

from __future__ import annotations

import json as json_lib
from typing import Any


class TestResponse:
    """Wraps an HTTP response with fluent assertion methods.

    All assert methods return ``self`` for chaining::

        response.assert_status(200).assert_json_path("$.id", exists=True)
    """

    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self._json: Any = ...

    def json(self) -> Any:
        """Parse and return response body as JSON."""
        if self._json is ...:
            self._json = json_lib.loads(self.body)
        return self._json

    def assert_status(self, expected: int) -> TestResponse:
        """Assert the response status code."""
        assert self.status_code == expected, f"Expected status {expected}, got {self.status_code}"
        return self

    def assert_json_path(self, path: str, *, value: Any = ..., exists: bool = True) -> TestResponse:
        """Assert a JSON path exists (or not) and optionally matches a value.

        ``jsonpath-ng`` is imported lazily (install ``pyfly[testing]``) so the rest
        of ``pyfly.testing`` stays importable without it.
        """
        try:
            from jsonpath_ng import parse as jsonpath_parse  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "assert_json_path requires jsonpath-ng; install it with `pip install pyfly[testing]`."
            ) from exc
        expr = jsonpath_parse(path)
        matches = expr.find(self.json())
        if exists:
            assert matches, f"No match for JSON path '{path}'"
            if value is not ...:
                assert matches[0].value == value, f"Expected {value!r} at '{path}', got {matches[0].value!r}"
        else:
            assert not matches, f"Expected no match for JSON path '{path}'"
        return self

    def assert_header(self, name: str, *, value: str | None = None, exists: bool = True) -> TestResponse:
        """Assert a response header exists (or not) and optionally matches a value."""
        header_val = self.headers.get(name.lower())
        if exists:
            assert header_val is not None, f"Header '{name}' not found"
            if value is not None:
                assert header_val == value, f"Expected header '{name}' = '{value}', got '{header_val}'"
        else:
            assert header_val is None, f"Header '{name}' should not exist"
        return self

    def assert_body_contains(self, text: str) -> TestResponse:
        """Assert the response body contains the given text."""
        body_str = self.body.decode("utf-8")
        assert text in body_str, f"Body does not contain '{text}'"
        return self


class PyFlyTestClient:
    """Test HTTP client wrapping Starlette's TestClient with fluent assertions.

    Usage::

        from starlette.applications import Starlette
        from pyfly.testing import PyFlyTestClient

        app = Starlette(routes=[...])
        client = PyFlyTestClient(app)
        client.get("/api/users").assert_status(200).assert_json_path("$[0].name", value="Alice")
    """

    def __init__(self, app: Any) -> None:
        from starlette.testclient import TestClient

        self._client = TestClient(app, raise_server_exceptions=False)

    def get(self, url: str, **kwargs: Any) -> TestResponse:
        """Send a GET request."""
        return self._wrap(self._client.get(url, **kwargs))

    def post(self, url: str, **kwargs: Any) -> TestResponse:
        """Send a POST request."""
        return self._wrap(self._client.post(url, **kwargs))

    def put(self, url: str, **kwargs: Any) -> TestResponse:
        """Send a PUT request."""
        return self._wrap(self._client.put(url, **kwargs))

    def delete(self, url: str, **kwargs: Any) -> TestResponse:
        """Send a DELETE request."""
        return self._wrap(self._client.delete(url, **kwargs))

    def patch(self, url: str, **kwargs: Any) -> TestResponse:
        """Send a PATCH request."""
        return self._wrap(self._client.patch(url, **kwargs))

    @staticmethod
    def _wrap(response: Any) -> TestResponse:
        return TestResponse(
            status_code=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
        )
