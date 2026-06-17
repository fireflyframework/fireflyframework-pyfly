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
"""Tests for ServerMetricsBinder."""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY, generate_latest

from pyfly.observability import server_metrics as sm
from pyfly.observability.server_metrics import ServerMetricsBinder, resolve_worker_count
from pyfly.server.ports.server_stats import ServerStats


@pytest.fixture(autouse=True)
def _fresh_collectors():
    sm.reset_collectors()
    yield
    sm.reset_collectors()


class _StubStatsPort:
    def __init__(self, active: int | None) -> None:
        self._active = active

    def sample(self) -> ServerStats:
        return ServerStats(workers=2, server_uptime_seconds=1.0, worker_pid=1, active_connections=self._active)

    def on_serve_start(self) -> None:  # pragma: no cover - not exercised here
        pass

    def on_serve_stop(self) -> None:  # pragma: no cover - not exercised here
        pass


class TestServerMetricsBinder:
    async def test_start_registers_meters(self) -> None:
        binder = ServerMetricsBinder(server_name="uvicorn", workers=3, sample_interval=60)
        await binder.start()
        try:
            exposition = generate_latest(REGISTRY).decode()
            assert "server_workers" in exposition
            assert "server_uptime_seconds" in exposition
            assert "server_started_total" in exposition
            assert 'server="uvicorn"' in exposition
            workers_value = sm._get_binder_collectors()["workers"].labels(*binder._labels)._value.get()
            assert workers_value == 3.0
        finally:
            await binder.stop()

    async def test_uptime_advances(self) -> None:
        binder = ServerMetricsBinder(server_name="uvicorn", workers=1, sample_interval=60)
        await binder.start()
        try:
            await asyncio.sleep(0.02)
            binder._refresh()
            value = sm._get_binder_collectors()["uptime"].labels(*binder._labels)._value.get()
            assert value > 0.0
        finally:
            await binder.stop()

    async def test_stop_marks_stopped_and_cancels_task(self) -> None:
        binder = ServerMetricsBinder(server_name="uvicorn", workers=1, sample_interval=0.01)
        await binder.start()
        await binder.stop()
        assert binder._task is None
        value = sm._get_binder_collectors()["stopped"].labels(*binder._labels)._value.get()
        assert value == 1.0

    async def test_native_connections_set_when_sample_provides(self) -> None:
        binder = ServerMetricsBinder(
            server_name="uvicorn", workers=1, stats_port=_StubStatsPort(active=11), sample_interval=60
        )
        await binder.start()
        try:
            value = sm._get_binder_collectors()["native_conns"].labels(*binder._labels)._value.get()
            assert value == 11.0
        finally:
            await binder.stop()

    async def test_native_connections_untouched_when_sample_none(self) -> None:
        binder = ServerMetricsBinder(
            server_name="granian", workers=1, stats_port=_StubStatsPort(active=None), sample_interval=60
        )
        await binder.start()
        try:
            value = sm._get_binder_collectors()["native_conns"].labels(*binder._labels)._value.get()
            assert value == 0.0
        finally:
            await binder.stop()

    async def test_double_start_does_not_raise_duplicated_timeseries(self) -> None:
        b1 = ServerMetricsBinder(server_name="uvicorn", workers=1, sample_interval=60)
        b2 = ServerMetricsBinder(server_name="uvicorn", workers=1, sample_interval=60)
        await b1.start()
        await b2.start()  # must reuse the process-global collectors, not re-register
        await b1.stop()
        await b2.stop()


class TestResolveWorkerCount:
    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_PYFLY_WORKERS", "4")
        assert resolve_worker_count() == 4

    def test_fallback_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_PYFLY_WORKERS", raising=False)
        assert resolve_worker_count(fallback=2) == 2

    def test_bad_value_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_PYFLY_WORKERS", "not-an-int")
        assert resolve_worker_count(fallback=1) == 1
