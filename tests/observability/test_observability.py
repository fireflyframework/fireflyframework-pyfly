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
"""Tests for observability module: metrics, tracing, and health aggregator."""

import pytest

from pyfly.actuator.health import HealthAggregator, HealthStatus
from pyfly.observability.metrics import MetricsRegistry, counted, timed
from pyfly.observability.tracing import span


class TestMetrics:
    def test_registry_creates_counter(self):
        registry = MetricsRegistry()
        counter = registry.counter("test_requests_total", "Total requests")
        counter.inc()
        assert counter._value.get() == 1.0

    def test_registry_creates_histogram(self):
        registry = MetricsRegistry()
        histogram = registry.histogram("test_duration_seconds", "Request duration")
        histogram.observe(0.5)

    def test_registry_creates_gauge(self):
        registry = MetricsRegistry()
        gauge = registry.gauge("test_gauge_active_connections", "Active connections")
        gauge.inc()
        gauge.inc()
        gauge.dec()
        assert gauge._value.get() == 1.0

    @pytest.mark.asyncio
    async def test_timed_decorator_micrometer_naming_and_tags(self):
        registry = MetricsRegistry()

        # Micrometer dot.case name -> Prometheus <name>_seconds timer.
        @timed(registry, "operation.duration", "Operation duration")
        async def slow_operation() -> str:
            return "done"

        assert await slow_operation() == "done"

        from prometheus_client import REGISTRY, generate_latest

        exposition = generate_latest(REGISTRY).decode()
        # Micrometer naming (_seconds timer) + class/method/exception tags.
        assert "operation_duration_seconds_count{" in exposition
        assert 'method="slow_operation"' in exposition
        assert 'exception="none"' in exposition
        assert 'operation_duration_seconds_count{class="",exception="none",method="slow_operation"} 1.0' in exposition

    @pytest.mark.asyncio
    async def test_counted_decorator_success_and_failure(self):
        registry = MetricsRegistry()

        @counted(registry, "operation.calls", "Operation calls")
        async def my_operation(fail: bool = False) -> str:
            if fail:
                raise RuntimeError("boom")
            return "ok"

        await my_operation()
        await my_operation()

        counter = registry._counters["operation_calls"]  # prometheus appends _total
        success = counter.labels(method="my_operation", result="success", exception="none", **{"class": ""})
        assert success._value.get() == 2.0

        with pytest.raises(RuntimeError):
            await my_operation(fail=True)
        failure = counter.labels(method="my_operation", result="failure", exception="RuntimeError", **{"class": ""})
        assert failure._value.get() == 1.0


class TestTracing:
    @pytest.mark.asyncio
    async def test_span_decorator(self):
        @span("process-order")
        async def process_order(order_id: str) -> dict:
            return {"id": order_id, "status": "processed"}

        result = await process_order("123")
        assert result == {"id": "123", "status": "processed"}

    @pytest.mark.asyncio
    async def test_span_propagates_exceptions(self):
        @span("failing-op")
        async def failing():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await failing()


class TestHealthAggregator:
    @pytest.mark.asyncio
    async def test_healthy_when_no_indicators(self):
        aggregator = HealthAggregator()
        result = await aggregator.check()
        assert result.status == "UP"

    @pytest.mark.asyncio
    async def test_healthy_with_passing_indicator(self):
        aggregator = HealthAggregator()

        class DbIndicator:
            async def health(self) -> HealthStatus:
                return HealthStatus(status="UP")

        aggregator.add_indicator("database", DbIndicator())
        result = await aggregator.check()
        assert result.status == "UP"
        assert result.components["database"].status == "UP"

    @pytest.mark.asyncio
    async def test_unhealthy_with_failing_indicator(self):
        aggregator = HealthAggregator()

        class RedisIndicator:
            async def health(self) -> HealthStatus:
                return HealthStatus(status="DOWN")

        aggregator.add_indicator("redis", RedisIndicator())
        result = await aggregator.check()
        assert result.status == "DOWN"
        assert result.components["redis"].status == "DOWN"

    @pytest.mark.asyncio
    async def test_unhealthy_when_indicator_raises(self):
        aggregator = HealthAggregator()

        class BrokenIndicator:
            async def health(self) -> HealthStatus:
                raise ConnectionError("can't connect")

        aggregator.add_indicator("kafka", BrokenIndicator())
        result = await aggregator.check()
        assert result.status == "DOWN"
        assert result.components["kafka"].status == "DOWN"

    @pytest.mark.asyncio
    async def test_mixed_indicators(self):
        aggregator = HealthAggregator()

        class OkIndicator:
            async def health(self) -> HealthStatus:
                return HealthStatus(status="UP")

        class BadIndicator:
            async def health(self) -> HealthStatus:
                return HealthStatus(status="DOWN")

        aggregator.add_indicator("db", OkIndicator())
        aggregator.add_indicator("redis", BadIndicator())
        result = await aggregator.check()
        assert result.status == "DOWN"
        assert result.components["db"].status == "UP"
        assert result.components["redis"].status == "DOWN"
