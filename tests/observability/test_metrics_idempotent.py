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
"""Process-global, idempotent metric registration (v26.06.97).

Two ``MetricsRegistry`` instances (e.g. a second ``create_app()`` in one pytest
process) used to re-create already-registered prometheus collectors and raise
"Duplicated timeseries in CollectorRegistry". Collector caches are now module-level,
so get-or-create is idempotent across every registry in the process.
"""

from __future__ import annotations

import pytest

pytest.importorskip("prometheus_client")

from pyfly.observability.metrics import MetricsRegistry  # noqa: E402


def test_two_registries_share_the_same_counter() -> None:
    # The exact scenario that used to raise "Duplicated timeseries".
    name = "pyfly_test_idempotent_counter_total"
    first = MetricsRegistry()
    second = MetricsRegistry()

    c1 = first.counter(name, "first registry counter", ["route"])
    c2 = second.counter(name, "second registry counter", ["route"])

    assert c1 is c2


def test_two_registries_share_the_same_histogram() -> None:
    name = "pyfly_test_idempotent_histogram_seconds"
    first = MetricsRegistry()
    second = MetricsRegistry()

    h1 = first.histogram(name, "first registry histogram", ["op"], buckets=(0.1, 0.5))
    h2 = second.histogram(name, "second registry histogram", ["op"], buckets=(0.1, 0.5))

    assert h1 is h2


def test_two_registries_share_the_same_gauge() -> None:
    name = "pyfly_test_idempotent_gauge"
    first = MetricsRegistry()
    second = MetricsRegistry()

    g1 = first.gauge(name, "first registry gauge", ["pool"])
    g2 = second.gauge(name, "second registry gauge", ["pool"])

    assert g1 is g2


def test_second_registry_does_not_raise_duplicated_timeseries() -> None:
    # Creating every collector type twice across two registries must not raise.
    first = MetricsRegistry()
    second = MetricsRegistry()

    first.counter("pyfly_test_dup_counter_total", "c", ["a"])
    first.histogram("pyfly_test_dup_histogram_seconds", "h", ["a"])
    first.gauge("pyfly_test_dup_gauge", "g", ["a"])

    # Would previously raise ValueError("Duplicated timeseries in CollectorRegistry: ...").
    second.counter("pyfly_test_dup_counter_total", "c", ["a"])
    second.histogram("pyfly_test_dup_histogram_seconds", "h", ["a"])
    second.gauge("pyfly_test_dup_gauge", "g", ["a"])
