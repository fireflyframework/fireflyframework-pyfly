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
"""Tests for saga and TCC configuration properties."""

from __future__ import annotations

import dataclasses

from pyfly.transactional.saga.config.properties import (
    BackpressureProperties,
    SagaEngineProperties,
)
from pyfly.transactional.tcc.config.properties import TccEngineProperties

# ---------------------------------------------------------------------------
# SagaEngineProperties
# ---------------------------------------------------------------------------


class TestSagaEngineProperties:
    """SagaEngineProperties holds saga engine configuration with sensible defaults."""

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(SagaEngineProperties)

    def test_defaults(self) -> None:
        props = SagaEngineProperties()

        assert props.enabled is True
        assert props.compensation_policy == "STRICT_SEQUENTIAL"
        assert props.default_timeout_ms == 300_000
        assert props.max_concurrent_sagas == 100
        assert props.layer_concurrency == 0
        assert props.persistence_enabled is True
        assert props.metrics_enabled is True
        assert props.recovery_enabled is True
        assert props.recovery_interval_seconds == 60
        assert props.stale_threshold_seconds == 600
        assert props.cleanup_older_than_hours == 24

    def test_custom_values(self) -> None:
        props = SagaEngineProperties(
            enabled=False,
            compensation_policy="PARALLEL",
            default_timeout_ms=60_000,
            max_concurrent_sagas=50,
            layer_concurrency=5,
            persistence_enabled=False,
            metrics_enabled=False,
            recovery_enabled=False,
            recovery_interval_seconds=120,
            stale_threshold_seconds=1200,
            cleanup_older_than_hours=48,
        )

        assert props.enabled is False
        assert props.compensation_policy == "PARALLEL"
        assert props.default_timeout_ms == 60_000
        assert props.max_concurrent_sagas == 50
        assert props.layer_concurrency == 5
        assert props.persistence_enabled is False
        assert props.metrics_enabled is False
        assert props.recovery_enabled is False
        assert props.recovery_interval_seconds == 120
        assert props.stale_threshold_seconds == 1200
        assert props.cleanup_older_than_hours == 48

    def test_is_mutable(self) -> None:
        """Config dataclasses must not be frozen so values can be overridden."""
        props = SagaEngineProperties()
        props.enabled = False
        props.compensation_policy = "PARALLEL"
        props.default_timeout_ms = 10_000

        assert props.enabled is False
        assert props.compensation_policy == "PARALLEL"
        assert props.default_timeout_ms == 10_000


# ---------------------------------------------------------------------------
# TccEngineProperties
# ---------------------------------------------------------------------------


class TestTccEngineProperties:
    """TccEngineProperties holds TCC engine configuration with sensible defaults."""

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(TccEngineProperties)

    def test_defaults(self) -> None:
        props = TccEngineProperties()

        assert props.enabled is True
        assert props.default_timeout_ms == 30_000
        assert props.retry_enabled is True
        assert props.max_retries == 3
        assert props.backoff_ms == 1_000
        assert props.persistence_enabled is True
        assert props.metrics_enabled is True

    def test_custom_values(self) -> None:
        props = TccEngineProperties(
            enabled=False,
            default_timeout_ms=15_000,
            retry_enabled=False,
            max_retries=5,
            backoff_ms=2_000,
            persistence_enabled=False,
            metrics_enabled=False,
        )

        assert props.enabled is False
        assert props.default_timeout_ms == 15_000
        assert props.retry_enabled is False
        assert props.max_retries == 5
        assert props.backoff_ms == 2_000
        assert props.persistence_enabled is False
        assert props.metrics_enabled is False

    def test_is_mutable(self) -> None:
        """Config dataclasses must not be frozen so values can be overridden."""
        props = TccEngineProperties()
        props.enabled = False
        props.default_timeout_ms = 5_000
        props.max_retries = 10

        assert props.enabled is False
        assert props.default_timeout_ms == 5_000
        assert props.max_retries == 10


# ---------------------------------------------------------------------------
# BackpressureProperties
# ---------------------------------------------------------------------------


class TestBackpressureProperties:
    """BackpressureProperties holds backpressure strategy configuration."""

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(BackpressureProperties)

    def test_defaults(self) -> None:
        props = BackpressureProperties()

        assert props.strategy == "adaptive"
        assert props.concurrency == 10
        assert props.batch_size == 5
        assert props.failure_threshold == 50
        assert props.success_threshold == 2
        assert props.wait_duration_ms == 60_000

    def test_custom_values(self) -> None:
        props = BackpressureProperties(
            strategy="fixed",
            concurrency=20,
            batch_size=10,
            failure_threshold=80,
            success_threshold=5,
            wait_duration_ms=120_000,
        )

        assert props.strategy == "fixed"
        assert props.concurrency == 20
        assert props.batch_size == 10
        assert props.failure_threshold == 80
        assert props.success_threshold == 5
        assert props.wait_duration_ms == 120_000

    def test_is_mutable(self) -> None:
        """Config dataclasses must not be frozen so values can be overridden."""
        props = BackpressureProperties()
        props.strategy = "fixed"
        props.concurrency = 50

        assert props.strategy == "fixed"
        assert props.concurrency == 50
