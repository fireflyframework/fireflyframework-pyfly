# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Incurred usage remains observable when enforcing a hard budget."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from pyfly.agentic.exceptions import BudgetExceededError
from pyfly.agentic.observability.budget import BudgetGate, BudgetRule
from pyfly.agentic.observability.metrics import AgentMetrics
from pyfly.agentic.observability.sinks import OTelMetricsSink
from pyfly.agentic.observability.usage import UsageRecord, UsageTracker


@pytest.mark.parametrize("fail_sink", [False, True])
def test_budget_breach_delivers_incurred_usage_to_every_sink(monkeypatch, fail_sink):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    monkeypatch.setattr("pyfly.agentic.observability.sinks.default_metrics", AgentMetrics(meter_provider=provider))
    delivered = []

    class Sink:
        def __init__(self, name):
            self.name = name

        def emit(self, record):
            delivered.append((self.name, record))
            if self.name == "first" and fail_sink:
                raise RuntimeError("sink unavailable")

        def flush(self):
            return None

        def close(self):
            return None

    gate = BudgetGate([BudgetRule(name="limit", limit_usd=0.5)])
    tracker = UsageTracker(gate=gate, sinks=[Sink("first"), OTelMetricsSink(), Sink("last")])
    record = UsageRecord(
        agent="worker",
        model="local",
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        cost_usd=1.0,
        cost_status="known",
    )
    try:
        with pytest.raises(BudgetExceededError) as caught:
            tracker.record(record)
        assert caught.value.rule_name == "limit"
        assert caught.value.spend_usd == 1.0
        assert caught.value.limit_usd == 0.5
        assert tracker.records == [record]
        assert tracker.cumulative_cost_usd == gate.spend("limit") == 1.0
        assert delivered == [("first", record), ("last", record)]
        data = reader.get_metrics_data()
        assert data is not None
        metrics = {
            metric.name: metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        for name, expected in {
            "pyfly.agentic.tokens.total": 18,
            "pyfly.agentic.tokens.prompt": 11,
            "pyfly.agentic.tokens.completion": 7,
            "pyfly.agentic.cost.total": 1.0,
            "pyfly.agentic.cost.observations": 1,
        }.items():
            assert sum(point.value for point in metrics[name].data.data_points) == expected
        assert metrics["pyfly.agentic.cost.observations"].data.data_points[0].attributes == {"status": "known"}
        if fail_sink:
            errors = metrics["pyfly.agentic.errors.total"].data.data_points
            assert sum(point.value for point in errors) == 1
            assert errors[0].attributes["operation"] == "cost_sink_errors"
    finally:
        provider.shutdown()
