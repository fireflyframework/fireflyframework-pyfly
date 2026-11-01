# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Known-price coverage is additive and never changes the legacy numeric ledger."""

import pytest

from pyfly.agentic.observability.usage import UsageRecord, UsageTracker


def test_legacy_manual_cost_is_not_invented_as_a_resolved_price():
    tracker = UsageTracker()
    tracker.record(UsageRecord(agent="legacy", model="fixture", cost_usd=5.0))
    summary = tracker.get_summary()
    assert summary.total_cost_usd == tracker.cumulative_cost_usd == 5.0
    assert getattr(summary, "cost_status", None) == "unknown"
    assert getattr(summary, "known_cost_usd", None) is None
    assert getattr(summary, "unknown_cost_records", None) == 1


def test_mixed_prices_share_the_same_coverage_contract_across_summary_dimensions():
    prices = {"zero": 0.0, "paid": 0.25}
    tracker = UsageTracker(resolver=lambda context: prices.get(context.model))
    for model in ["zero", "paid", "unresolved"]:
        tracker.record_call(model=model, agent="fixture", correlation_id="run", input_tokens=1, output_tokens=1)
    for summary in [
        tracker.get_summary(),
        tracker.get_summary_for_agent("fixture"),
        tracker.get_summary_for_correlation("run"),
    ]:
        assert summary.total_cost_usd == 0.25
        assert getattr(summary, "cost_status", None) == "partial"
        assert getattr(summary, "known_cost_usd", None) == 0.25
        assert getattr(summary, "unknown_cost_records", None) == 1
    by_model = tracker.get_summary().by_model
    assert by_model["zero"].get("cost_status") == "known" and by_model["zero"].get("known_cost_usd") == 0.0
    assert by_model["unresolved"].get("cost_status") == "unknown"


@pytest.mark.parametrize("value", [True, -1.0, float("nan"), float("inf")])
def test_invalid_custom_resolver_results_never_become_known_prices(value):
    tracker = UsageTracker(resolver=lambda context: value)
    record = tracker.record_call(model="fixture", input_tokens=1, output_tokens=1)
    assert record.cost_usd == 0.0
    assert getattr(record, "cost_status", None) == "unknown"
    assert getattr(tracker.get_summary(), "known_cost_usd", None) is None


def test_coverage_tracks_retained_observations_without_resetting_cumulative_spend():
    tracker = UsageTracker(max_records=1, resolver=lambda context: 0.25 if context.model == "paid" else None)
    assert getattr(tracker.get_summary(), "cost_status", None) == "unavailable"
    tracker.record_call(model="paid", input_tokens=1, output_tokens=1)
    tracker.record_call(model="unknown", input_tokens=1, output_tokens=1)
    assert tracker.get_summary().total_cost_usd == 0.0
    assert tracker.cumulative_cost_usd == 0.25
    assert getattr(tracker.get_summary(), "cost_status", None) == "unknown"


def test_cost_coverage_otel_fact_has_only_a_bounded_status_label():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from pyfly.agentic.observability.metrics import AgentMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    try:
        metrics = AgentMetrics(meter_provider=provider)
        assert hasattr(metrics, "record_cost_observation")
        metrics.record_cost_observation("known")
        metrics.record_cost_observation("unknown")
        with pytest.raises(ValueError):
            metrics.record_cost_observation("PRIVATE-invalid-status")
        samples = [
            metric
            for resource in reader.get_metrics_data().resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == "pyfly.agentic.cost.observations"
        ]
        assert len(samples) == 1
        assert {tuple(point.attributes.items()) for point in samples[0].data.data_points} == {
            (("status", "known"),),
            (("status", "unknown"),),
        }
    finally:
        provider.shutdown()
