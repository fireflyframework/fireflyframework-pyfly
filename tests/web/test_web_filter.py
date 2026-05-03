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
"""Tests for WebFilter Protocol and OncePerRequestFilter base class."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import WebFilter

# ---------------------------------------------------------------------------
# WebFilter Protocol
# ---------------------------------------------------------------------------


class _DuckFilter:
    """Implements WebFilter via duck typing (no inheritance)."""

    async def do_filter(self, request, call_next):
        return await call_next(request)

    def should_not_filter(self, request) -> bool:
        return False


class TestWebFilterProtocol:
    def test_duck_typed_class_is_webfilter(self):
        assert isinstance(_DuckFilter(), WebFilter)

    def test_once_per_request_filter_is_webfilter(self):
        class _Concrete(OncePerRequestFilter):
            async def do_filter(self, request, call_next):
                return await call_next(request)

        assert isinstance(_Concrete(), WebFilter)

    def test_non_filter_is_not_webfilter(self):
        class _NotAFilter:
            pass

        assert not isinstance(_NotAFilter(), WebFilter)


# ---------------------------------------------------------------------------
# OncePerRequestFilter — should_not_filter
# ---------------------------------------------------------------------------


def _make_request(path: str) -> MagicMock:
    """Create a mock request with the given URL path."""
    req = MagicMock()
    req.url.path = path
    return req


class _TestFilter(OncePerRequestFilter):
    async def do_filter(self, request, call_next):
        return await call_next(request)


class TestOncePerRequestFilterMatching:
    def test_no_patterns_matches_all(self):
        f = _TestFilter()
        assert not f.should_not_filter(_make_request("/anything"))
        assert not f.should_not_filter(_make_request("/api/v1/users"))

    def test_url_patterns_match(self):
        f = _TestFilter()
        f.url_patterns = ["/api/*"]
        assert not f.should_not_filter(_make_request("/api/users"))
        assert f.should_not_filter(_make_request("/health"))

    def test_url_patterns_multiple(self):
        f = _TestFilter()
        f.url_patterns = ["/api/*", "/admin/*"]
        assert not f.should_not_filter(_make_request("/api/users"))
        assert not f.should_not_filter(_make_request("/admin/dashboard"))
        assert f.should_not_filter(_make_request("/public/index"))

    def test_exclude_patterns(self):
        f = _TestFilter()
        f.exclude_patterns = ["/health", "/actuator/*"]
        assert f.should_not_filter(_make_request("/health"))
        assert f.should_not_filter(_make_request("/actuator/info"))
        assert not f.should_not_filter(_make_request("/api/users"))

    def test_url_and_exclude_patterns_combined(self):
        f = _TestFilter()
        f.url_patterns = ["/api/*"]
        f.exclude_patterns = ["/api/public/*"]
        # Matches url_patterns but excluded
        assert f.should_not_filter(_make_request("/api/public/docs"))
        # Matches url_patterns, not excluded
        assert not f.should_not_filter(_make_request("/api/users"))
        # Doesn't match url_patterns at all
        assert f.should_not_filter(_make_request("/health"))

    def test_instance_patterns_do_not_leak_to_class(self):
        """Setting patterns on an instance should not mutate the class defaults."""
        f1 = _TestFilter()
        f1.url_patterns = ["/api/*"]
        f2 = _TestFilter()
        assert f2.url_patterns == []


class TestOncePerRequestFilterAbstract:
    def test_cannot_instantiate_without_do_filter(self):
        with pytest.raises(TypeError):
            OncePerRequestFilter()  # type: ignore[abstract]
