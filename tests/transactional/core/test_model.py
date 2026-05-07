# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for core orchestration model enums and value types."""

from __future__ import annotations

from pyfly.transactional.core.model import (
    BackpressureProperties,
    CircuitBreakerConfig,
    CompensationPolicy,
    ExecutionPattern,
    ExecutionStatus,
    RetryPolicy,
    StepStatus,
    TccPhase,
    TriggerMode,
)


class TestExecutionStatus:
    def test_terminal_states(self) -> None:
        terminal = {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CONFIRMED,
            ExecutionStatus.CANCELED,
            ExecutionStatus.COMPENSATED,
        }
        for status in ExecutionStatus:
            assert status.is_terminal == (status in terminal)

    def test_running_is_not_terminal(self) -> None:
        assert not ExecutionStatus.RUNNING.is_terminal
        assert not ExecutionStatus.PENDING.is_terminal


class TestEnums:
    def test_pattern_values(self) -> None:
        assert ExecutionPattern.SAGA.value == "SAGA"
        assert ExecutionPattern.WORKFLOW.value == "WORKFLOW"
        assert ExecutionPattern.TCC.value == "TCC"

    def test_trigger_mode(self) -> None:
        assert TriggerMode.SYNC.value == "SYNC"
        assert TriggerMode.ASYNC.value == "ASYNC"

    def test_step_status_values(self) -> None:
        assert StepStatus.PENDING in StepStatus
        assert StepStatus.COMPENSATED in StepStatus
        assert StepStatus.COMPENSATION_FAILED in StepStatus

    def test_compensation_policy_values(self) -> None:
        names = {p.value for p in CompensationPolicy}
        assert "STRICT_SEQUENTIAL" in names
        assert "BEST_EFFORT_PARALLEL" in names

    def test_tcc_phase(self) -> None:
        assert {p.value for p in TccPhase} == {"TRY", "CONFIRM", "CANCEL"}


class TestRetryPolicy:
    def test_default(self) -> None:
        p = RetryPolicy()
        assert p.max_attempts == 1
        assert p.backoff_ms == 0

    def test_custom(self) -> None:
        p = RetryPolicy(max_attempts=3, backoff_ms=100, jitter=True, jitter_factor=0.2, timeout_ms=5000)
        assert p.max_attempts == 3
        assert p.timeout_ms == 5000
        assert p.jitter is True


class TestBackpressureProperties:
    def test_defaults(self) -> None:
        bp = BackpressureProperties()
        assert bp.strategy == "adaptive"
        assert bp.batch_size == 10
        assert isinstance(bp.circuit_breaker, CircuitBreakerConfig)
        assert bp.circuit_breaker.failure_threshold == 5
