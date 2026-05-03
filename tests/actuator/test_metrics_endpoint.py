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
"""Tests for the MetricsEndpoint — Prometheus registry integration."""

from __future__ import annotations

import contextlib

import pytest
from prometheus_client import REGISTRY, Counter

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

    def test_enabled_by_default(self) -> None:
        ep = MetricsEndpoint()
        assert ep.enabled is True

    @pytest.mark.asyncio
    async def test_list_metrics_returns_names(self) -> None:
        counter = Counter("test_me_total", "test counter")
        counter.inc()
        ep = MetricsEndpoint()

        data = await ep.handle()

        assert "names" in data
        assert isinstance(data["names"], list)
        assert "test_me_total" in data["names"]

    @pytest.mark.asyncio
    async def test_detail_returns_measurements(self) -> None:
        counter = Counter("test_me_detail_total", "test detail counter", ["method"])
        counter.labels(method="GET").inc(5)
        ep = MetricsEndpoint()

        data = await ep.handle(context={"name": "test_me_detail_total"})

        assert data["name"] == "test_me_detail_total"
        assert len(data["measurements"]) > 0
        assert any(m["value"] == 5.0 for m in data["measurements"])
