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
"""Tests for the MetricsEndpoint — Spring Boot / Micrometer JSON parity."""

from __future__ import annotations

import contextlib

import pytest
from prometheus_client import REGISTRY, Counter, Summary

from pyfly.actuator.endpoints import MetricsEndpoint


@pytest.fixture(autouse=True)
def _clean_test_metrics():
    """Clean up test metrics after each test."""
    yield
    for name in list(REGISTRY._names_to_collectors.keys()):
        if name.startswith("test_me_"):
            with contextlib.suppress(Exception):
                REGISTRY.unregister(REGISTRY._names_to_collectors[name])


class TestMetricsEndpoint:
    def test_endpoint_id(self) -> None:
        ep = MetricsEndpoint()
        assert ep.endpoint_id == "metrics"

    @pytest.mark.asyncio
    async def test_list_returns_dot_meter_names(self) -> None:
        # Prometheus family test_me_widgets_total -> Micrometer meter test.me.widgets
        counter = Counter("test_me_widgets_total", "test counter")
        counter.inc()
        ep = MetricsEndpoint()

        data = await ep.handle()

        assert "test.me.widgets" in data["names"]
        # Underscore prometheus names must NOT appear in the Micrometer list.
        assert "test_me_widgets_total" not in data["names"]

    @pytest.mark.asyncio
    async def test_detail_counter_uses_count_statistic(self) -> None:
        counter = Counter("test_me_orders_total", "orders", ["method"])
        counter.labels(method="GET").inc(5)
        ep = MetricsEndpoint()

        data = await ep.handle(context={"selector": "test.me.orders"})

        assert data["name"] == "test.me.orders"
        stats = {m["statistic"]: m["value"] for m in data["measurements"]}
        assert stats["COUNT"] == 5.0
        # availableTags exposes the label values.
        tags = {t["tag"]: t["values"] for t in data["availableTags"]}
        assert tags["method"] == ["GET"]

    @pytest.mark.asyncio
    async def test_detail_summary_count_sum_and_baseunit(self) -> None:
        s = Summary("test_me_latency_seconds", "latency", ["uri"])
        s.labels(uri="/a").observe(0.5)
        s.labels(uri="/a").observe(1.5)
        ep = MetricsEndpoint()

        data = await ep.handle(context={"selector": "test.me.latency"})

        stats = {m["statistic"]: m["value"] for m in data["measurements"]}
        assert stats["COUNT"] == 2.0
        assert stats["TOTAL_TIME"] == 2.0
        assert data["baseUnit"] == "seconds"

    @pytest.mark.asyncio
    async def test_detail_tag_filter(self) -> None:
        counter = Counter("test_me_hits_total", "hits", ["region"])
        counter.labels(region="eu").inc(3)
        counter.labels(region="us").inc(7)
        ep = MetricsEndpoint()

        data = await ep.handle(context={"selector": "test.me.hits", "query": {"tag": "region:eu"}})

        stats = {m["statistic"]: m["value"] for m in data["measurements"]}
        assert stats["COUNT"] == 3.0  # only the eu series

    @pytest.mark.asyncio
    async def test_unknown_meter_returns_none(self) -> None:
        ep = MetricsEndpoint()
        assert await ep.handle(context={"selector": "nope.does.not.exist"}) is None


class TestProcessMeters:
    def test_process_meters_registered_with_micrometer_names(self) -> None:
        from prometheus_client import generate_latest

        from pyfly.observability.process_metrics import register_process_metrics

        register_process_metrics()
        exposition = generate_latest(REGISTRY).decode()
        # Spring Boot / Micrometer names.
        assert "process_uptime_seconds" in exposition
        assert "process_start_time_seconds" in exposition
        assert "system_cpu_count" in exposition
        assert "process_cpu_usage" in exposition
