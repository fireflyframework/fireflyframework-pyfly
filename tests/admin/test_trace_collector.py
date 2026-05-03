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
"""Tests for HTTP trace collector."""

from unittest.mock import MagicMock

from pyfly.admin.middleware.trace_collector import TraceCollectorFilter


class TestTraceCollectorFilter:
    def test_excludes_admin_paths(self):
        f = TraceCollectorFilter()
        req = MagicMock()
        req.url.path = "/admin/api/beans"
        assert f.should_not_filter(req) is True

    def test_excludes_actuator_paths(self):
        f = TraceCollectorFilter()
        req = MagicMock()
        req.url.path = "/actuator/health"
        assert f.should_not_filter(req) is True

    def test_includes_app_paths(self):
        f = TraceCollectorFilter()
        req = MagicMock()
        req.url.path = "/api/users"
        assert f.should_not_filter(req) is False

    async def test_captures_trace(self):
        f = TraceCollectorFilter()
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/api/users"

        response = MagicMock()
        response.status_code = 200

        async def call_next(r):
            return response

        await f.do_filter(req, call_next)
        assert len(f.get_traces()) == 1
        trace = f.get_traces()[0]
        assert trace["method"] == "GET"
        assert trace["path"] == "/api/users"
        assert trace["status"] == 200

    async def test_max_traces_ring_buffer(self):
        f = TraceCollectorFilter(max_traces=2)
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/api/test"
        response = MagicMock()
        response.status_code = 200

        async def call_next(r):
            return response

        for _ in range(5):
            await f.do_filter(req, call_next)
        assert len(f.get_traces()) == 2
