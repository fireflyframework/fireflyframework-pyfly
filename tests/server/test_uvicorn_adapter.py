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
"""Tests for UvicornServerAdapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from pyfly.config.properties.server import ServerProperties
from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter
from pyfly.server.ports.outbound import ApplicationServerPort
from pyfly.server.ports.server_stats import ServerStats, ServerStatsPort
from pyfly.server.types import ServerInfo


class TestUvicornServerAdapter:
    def test_is_application_server_port(self):
        adapter = UvicornServerAdapter()
        assert isinstance(adapter, ApplicationServerPort)

    def test_server_info(self):
        adapter = UvicornServerAdapter()
        info = adapter.server_info
        assert isinstance(info, ServerInfo)
        assert info.name == "uvicorn"

    @patch("pyfly.server.adapters.uvicorn.adapter.uvicorn")
    def test_serve_calls_uvicorn_run(self, mock_uvicorn):
        adapter = UvicornServerAdapter()
        config = ServerProperties(type="uvicorn", workers=2)
        config.host = "0.0.0.0"
        config.port = 8000
        adapter.serve("myapp:app", config)

        mock_uvicorn.run.assert_called_once()

    def test_shutdown_does_not_raise(self):
        adapter = UvicornServerAdapter()
        adapter.shutdown()


class TestUvicornServerStats:
    def test_implements_server_stats_port(self):
        assert isinstance(UvicornServerAdapter(), ServerStatsPort)

    def test_sample_without_server_returns_none_connection_fields(self):
        adapter = UvicornServerAdapter()
        stats = adapter.sample()
        assert isinstance(stats, ServerStats)
        assert stats.active_connections is None
        assert stats.total_requests is None
        assert stats.workers == 1

    def test_on_serve_start_makes_uptime_advance(self):
        adapter = UvicornServerAdapter()
        adapter.on_serve_start()
        stats = adapter.sample()
        assert stats.server_uptime_seconds >= 0.0

    def test_sample_reads_server_state_when_present(self):
        adapter = UvicornServerAdapter()
        adapter._server = SimpleNamespace(server_state=SimpleNamespace(total_requests=5, connections={1, 2, 3}))
        adapter._info = ServerInfo(
            name="uvicorn", version="x", workers=4, event_loop="asyncio", http_protocol="h1", host="0.0.0.0", port=8000
        )
        stats = adapter.sample()
        assert stats.active_connections == 3
        assert stats.total_requests == 5
        assert stats.workers == 4

    def test_on_serve_stop_clears_global_active_server(self):
        from pyfly.server.adapters.uvicorn import adapter as mod

        adapter = UvicornServerAdapter()
        sentinel = SimpleNamespace(server_state=SimpleNamespace(total_requests=0, connections=set()))
        adapter._server = sentinel
        mod._active_server = sentinel
        adapter.on_serve_stop()
        assert mod._active_server is None
