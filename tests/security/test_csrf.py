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
"""Tests for CSRF double-submit cookie utilities and filter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import Response

from pyfly.security.csrf import generate_csrf_token, validate_csrf_token
from pyfly.web.adapters.starlette.filters.csrf_filter import CsrfFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    method: str = "GET",
    path: str = "/api/test",
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Build a lightweight mock request compatible with the filter."""
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies or {},
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# Token utility tests
# ---------------------------------------------------------------------------


class TestCsrfTokenUtilities:
    def test_generate_csrf_token(self) -> None:
        token = generate_csrf_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_validate_csrf_token_matching(self) -> None:
        token = generate_csrf_token()
        assert validate_csrf_token(token, token) is True

    def test_validate_csrf_token_mismatch(self) -> None:
        token_a = generate_csrf_token()
        token_b = generate_csrf_token()
        assert validate_csrf_token(token_a, token_b) is False


# ---------------------------------------------------------------------------
# CsrfFilter tests
# ---------------------------------------------------------------------------


class TestCsrfFilter:
    """Tests for :class:`CsrfFilter`."""

    @pytest.mark.asyncio
    async def test_csrf_filter_safe_method_sets_cookie(self) -> None:
        """GET request passes through and response has XSRF-TOKEN cookie."""
        csrf_filter = CsrfFilter()
        request = _make_request(method="GET")
        response = Response(content="ok", status_code=200)
        call_next = AsyncMock(return_value=response)

        result = await csrf_filter.do_filter(request, call_next)

        call_next.assert_awaited_once_with(request)
        assert result is response
        # Verify the XSRF-TOKEN cookie was set on the response
        cookie_header = result.headers.get("set-cookie", "")
        assert "XSRF-TOKEN" in cookie_header

    @pytest.mark.asyncio
    async def test_csrf_filter_unsafe_method_missing_cookie(self) -> None:
        """POST without CSRF cookie returns 403."""
        csrf_filter = CsrfFilter()
        request = _make_request(
            method="POST",
            headers={"X-XSRF-TOKEN": "some-token"},
        )
        call_next = AsyncMock()

        result = await csrf_filter.do_filter(request, call_next)

        assert result.status_code == 403
        call_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_csrf_filter_unsafe_method_missing_header(self) -> None:
        """POST with cookie but no header returns 403."""
        csrf_filter = CsrfFilter()
        request = _make_request(
            method="POST",
            cookies={"XSRF-TOKEN": "some-token"},
        )
        call_next = AsyncMock()

        result = await csrf_filter.do_filter(request, call_next)

        assert result.status_code == 403
        call_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_csrf_filter_unsafe_method_invalid_token(self) -> None:
        """POST with mismatched cookie/header returns 403."""
        csrf_filter = CsrfFilter()
        request = _make_request(
            method="POST",
            cookies={"XSRF-TOKEN": "token-a"},
            headers={"X-XSRF-TOKEN": "token-b"},
        )
        call_next = AsyncMock()

        result = await csrf_filter.do_filter(request, call_next)

        assert result.status_code == 403
        call_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_csrf_filter_unsafe_method_valid_token(self) -> None:
        """POST with matching cookie/header passes through."""
        csrf_filter = CsrfFilter()
        token = generate_csrf_token()
        request = _make_request(
            method="POST",
            cookies={"XSRF-TOKEN": token},
            headers={"X-XSRF-TOKEN": token},
        )
        response = Response(content="created", status_code=201)
        call_next = AsyncMock(return_value=response)

        result = await csrf_filter.do_filter(request, call_next)

        call_next.assert_awaited_once_with(request)
        assert result is response
        # A new CSRF cookie should be set (token rotation)
        cookie_header = result.headers.get("set-cookie", "")
        assert "XSRF-TOKEN" in cookie_header

    @pytest.mark.asyncio
    async def test_csrf_filter_bearer_bypass(self) -> None:
        """POST with Bearer Authorization header skips CSRF validation."""
        csrf_filter = CsrfFilter()
        request = _make_request(
            method="POST",
            headers={"authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.test.sig"},
        )
        response = Response(content="ok", status_code=200)
        call_next = AsyncMock(return_value=response)

        result = await csrf_filter.do_filter(request, call_next)

        call_next.assert_awaited_once_with(request)
        assert result is response
