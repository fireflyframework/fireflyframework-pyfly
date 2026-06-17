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
"""Tests for the ServerStatsPort protocol + ServerStats dataclass."""

from __future__ import annotations

import os

import pytest

from pyfly.server.ports.server_stats import ServerStats, ServerStatsPort


class _FakeStatsServer:
    """Duck-typed ServerStatsPort."""

    def sample(self) -> ServerStats | None:
        return ServerStats(workers=1, server_uptime_seconds=1.0, worker_pid=os.getpid())

    def on_serve_start(self) -> None:
        pass

    def on_serve_stop(self) -> None:
        pass


class _NotAStatsServer:
    def sample(self) -> None:  # missing on_serve_start/on_serve_stop
        return None


class TestServerStatsPort:
    def test_duck_typed_class_is_stats_port(self) -> None:
        assert isinstance(_FakeStatsServer(), ServerStatsPort)

    def test_incomplete_class_is_not_stats_port(self) -> None:
        assert not isinstance(_NotAStatsServer(), ServerStatsPort)


class TestServerStats:
    def test_optional_connection_fields_default_to_none(self) -> None:
        stats = ServerStats(workers=4, server_uptime_seconds=12.5, worker_pid=123)
        assert stats.active_connections is None
        assert stats.total_requests is None
        assert stats.workers == 4

    def test_is_frozen(self) -> None:
        stats = ServerStats(workers=1, server_uptime_seconds=0.0, worker_pid=1)
        with pytest.raises(AttributeError):
            stats.workers = 2  # type: ignore[misc]

    def test_populated_connection_fields(self) -> None:
        stats = ServerStats(workers=1, server_uptime_seconds=3.0, worker_pid=1, active_connections=7, total_requests=42)
        assert stats.active_connections == 7
        assert stats.total_requests == 42


class TestGranianAdapterStats:
    """Granian/Hypercorn adapters import without their server packages, so these
    run regardless of what is installed."""

    def test_granian_implements_port_and_samples_workers_only(self) -> None:
        from pyfly.server.adapters.granian.adapter import GranianServerAdapter

        adapter = GranianServerAdapter()
        assert isinstance(adapter, ServerStatsPort)
        adapter.on_serve_start()
        stats = adapter.sample()
        assert stats.active_connections is None
        assert stats.total_requests is None
        assert stats.server_uptime_seconds >= 0.0

    def test_hypercorn_implements_port_and_samples_workers_only(self) -> None:
        from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

        adapter = HypercornServerAdapter()
        assert isinstance(adapter, ServerStatsPort)
        adapter.on_serve_start()
        stats = adapter.sample()
        assert stats.active_connections is None
        assert stats.total_requests is None
        assert stats.server_uptime_seconds >= 0.0
