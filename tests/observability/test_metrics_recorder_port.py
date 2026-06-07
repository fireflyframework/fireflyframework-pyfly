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
"""MetricsRecorder port + NoOp adapter (v26.06.72)."""

from __future__ import annotations

from pyfly.observability import MetricsRecorder, NoOpMetricsRecorder


def test_noop_recorder_satisfies_port_and_is_inert() -> None:
    recorder: MetricsRecorder = NoOpMetricsRecorder()
    assert isinstance(recorder, MetricsRecorder)

    # Every create + every Prometheus-style op is a no-op that never raises.
    counter = recorder.counter("reqs", "requests", ["route"])
    counter.labels(route="/x").inc()
    counter.inc(3)
    recorder.gauge("inflight", "in flight").set(5)
    hist = recorder.histogram("latency", "latency seconds", buckets=(0.1, 0.5))
    hist.labels().observe(0.2)


def test_metrics_registry_is_a_metrics_recorder() -> None:
    # MetricsRegistry (Prometheus adapter) is a nominal MetricsRecorder, so application code can
    # depend on the port. Skip if prometheus_client isn't installed.
    try:
        from pyfly.observability.metrics import MetricsRegistry
    except ImportError:
        import pytest

        pytest.skip("prometheus_client not installed")
    assert issubclass(MetricsRegistry, MetricsRecorder)
    assert isinstance(MetricsRegistry(), MetricsRecorder)
