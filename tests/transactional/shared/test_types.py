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
"""Tests for shared transactional types — StepStatus, CompensationPolicy, BackpressureConfig."""

from __future__ import annotations

import pytest

from pyfly.transactional.shared.types import (
    BackpressureConfig,
    CompensationPolicy,
    StepStatus,
)


class TestStepStatus:
    def test_pending_value(self) -> None:
        assert StepStatus.PENDING == "PENDING"

    def test_running_value(self) -> None:
        assert StepStatus.RUNNING == "RUNNING"

    def test_done_value(self) -> None:
        assert StepStatus.DONE == "DONE"

    def test_failed_value(self) -> None:
        assert StepStatus.FAILED == "FAILED"

    def test_compensated_value(self) -> None:
        assert StepStatus.COMPENSATED == "COMPENSATED"

    def test_count_is_five(self) -> None:
        assert len(StepStatus) == 5

    def test_all_members_exist(self) -> None:
        members = {s.name for s in StepStatus}
        assert members == {"PENDING", "RUNNING", "DONE", "FAILED", "COMPENSATED"}

    def test_is_string_subclass(self) -> None:
        assert isinstance(StepStatus.PENDING, str)


class TestCompensationPolicy:
    def test_strict_sequential_value(self) -> None:
        assert CompensationPolicy.STRICT_SEQUENTIAL == "STRICT_SEQUENTIAL"

    def test_grouped_parallel_value(self) -> None:
        assert CompensationPolicy.GROUPED_PARALLEL == "GROUPED_PARALLEL"

    def test_retry_with_backoff_value(self) -> None:
        assert CompensationPolicy.RETRY_WITH_BACKOFF == "RETRY_WITH_BACKOFF"

    def test_circuit_breaker_value(self) -> None:
        assert CompensationPolicy.CIRCUIT_BREAKER == "CIRCUIT_BREAKER"

    def test_best_effort_parallel_value(self) -> None:
        assert CompensationPolicy.BEST_EFFORT_PARALLEL == "BEST_EFFORT_PARALLEL"

    def test_count_is_five(self) -> None:
        assert len(CompensationPolicy) == 5

    def test_all_members_exist(self) -> None:
        members = {p.name for p in CompensationPolicy}
        assert members == {
            "STRICT_SEQUENTIAL",
            "GROUPED_PARALLEL",
            "RETRY_WITH_BACKOFF",
            "CIRCUIT_BREAKER",
            "BEST_EFFORT_PARALLEL",
        }

    def test_is_string_subclass(self) -> None:
        assert isinstance(CompensationPolicy.STRICT_SEQUENTIAL, str)


class TestBackpressureConfig:
    def test_strategy_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.strategy == "adaptive"

    def test_concurrency_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.concurrency == 10

    def test_batch_size_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.batch_size == 5

    def test_failure_threshold_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.failure_threshold == 50

    def test_success_threshold_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.success_threshold == 2

    def test_wait_duration_ms_default(self) -> None:
        cfg = BackpressureConfig()
        assert cfg.wait_duration_ms == 60000

    def test_is_frozen_raises_on_strategy_mutation(self) -> None:
        cfg = BackpressureConfig()
        with pytest.raises(AttributeError):
            cfg.strategy = "fixed"  # type: ignore[misc]

    def test_is_frozen_raises_on_concurrency_mutation(self) -> None:
        cfg = BackpressureConfig()
        with pytest.raises(AttributeError):
            cfg.concurrency = 20  # type: ignore[misc]

    def test_custom_values_are_stored(self) -> None:
        cfg = BackpressureConfig(
            strategy="fixed",
            concurrency=4,
            batch_size=2,
            failure_threshold=10,
            success_threshold=1,
            wait_duration_ms=5000,
        )
        assert cfg.strategy == "fixed"
        assert cfg.concurrency == 4
        assert cfg.batch_size == 2
        assert cfg.failure_threshold == 10
        assert cfg.success_threshold == 1
        assert cfg.wait_duration_ms == 5000
