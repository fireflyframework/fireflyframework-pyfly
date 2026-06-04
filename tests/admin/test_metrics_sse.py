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
"""Tests for the admin metrics SSE stream carrying live values (not just names)."""

from __future__ import annotations

import contextlib
import json

import pytest
from prometheus_client import REGISTRY, Counter

from pyfly.admin.api.sse import metrics_stream
from pyfly.admin.providers.metrics_provider import MetricsProvider


@pytest.fixture(autouse=True)
def _clean():
    yield
    for name in list(REGISTRY._names_to_collectors.keys()):
        if name.startswith("test_sse_"):
            with contextlib.suppress(Exception):
                REGISTRY.unregister(REGISTRY._names_to_collectors[name])


class TestMetricValues:
    @pytest.mark.asyncio
    async def test_get_metric_values_returns_summed_values(self):
        counter = Counter("test_sse_hits_total", "hits", ["region"])
        counter.labels(region="eu").inc(2)
        counter.labels(region="us").inc(3)

        data = await MetricsProvider().get_metric_values()

        assert data["available"] is True
        # Summed across label sets -> single trend value per metric name.
        assert data["values"]["test_sse_hits_total"] == 5.0
        assert "test_sse_hits_total" in data["names"]

    @pytest.mark.asyncio
    async def test_metrics_stream_emits_values_event(self):
        Counter("test_sse_widgets_total", "widgets").inc(7)

        gen = metrics_stream(MetricsProvider(), interval=0.01)
        try:
            event = await anext(gen)
        finally:
            await gen.aclose()

        assert "event: metrics" in event
        # Extract the JSON payload from the SSE frame and verify values present.
        payload = json.loads(next(line[len("data: ") :] for line in event.splitlines() if line.startswith("data: ")))
        assert payload["values"]["test_sse_widgets_total"] == 7.0
