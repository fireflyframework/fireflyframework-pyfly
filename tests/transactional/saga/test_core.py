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
"""Tests for saga core types — SagaContext, SagaResult, StepOutcome, FailureReport."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.report import FailureReport
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
from pyfly.transactional.shared.types import StepStatus

# ── helpers ───────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _make_step_outcome(
    status: StepStatus = StepStatus.DONE,
    result: object = "ok",
    error: Exception | None = None,
    compensated: bool = False,
    compensation_result: object | None = None,
    compensation_error: Exception | None = None,
) -> StepOutcome:
    return StepOutcome(
        status=status,
        attempts=1,
        latency_ms=12.5,
        result=result,
        error=error,
        compensated=compensated,
        started_at=_now(),
        compensation_result=compensation_result,
        compensation_error=compensation_error,
    )


# ── SagaContext ───────────────────────────────────────────────


class TestSagaContextDefaults:
    def test_correlation_id_is_generated(self) -> None:
        ctx = SagaContext()
        assert isinstance(ctx.correlation_id, str)
        assert len(ctx.correlation_id) > 0

    def test_correlation_id_is_unique(self) -> None:
        ctx1 = SagaContext()
        ctx2 = SagaContext()
        assert ctx1.correlation_id != ctx2.correlation_id

    def test_saga_name_defaults_to_empty_string(self) -> None:
        ctx = SagaContext()
        assert ctx.saga_name == ""

    def test_headers_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.headers == {}

    def test_variables_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.variables == {}

    def test_step_results_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_results == {}

    def test_step_statuses_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_statuses == {}

    def test_step_attempts_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_attempts == {}

    def test_step_latencies_ms_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_latencies_ms == {}

    def test_step_started_at_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_started_at == {}

    def test_compensation_results_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.compensation_results == {}

    def test_compensation_errors_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.compensation_errors == {}

    def test_idempotency_keys_default_to_empty_set(self) -> None:
        ctx = SagaContext()
        assert ctx.idempotency_keys == set()

    def test_topology_layers_default_to_empty_list(self) -> None:
        ctx = SagaContext()
        assert ctx.topology_layers == []

    def test_step_dependencies_default_to_empty_dict(self) -> None:
        ctx = SagaContext()
        assert ctx.step_dependencies == {}


class TestSagaContextCustomInit:
    def test_custom_correlation_id(self) -> None:
        ctx = SagaContext(correlation_id="corr-abc")
        assert ctx.correlation_id == "corr-abc"

    def test_custom_saga_name(self) -> None:
        ctx = SagaContext(saga_name="order-saga")
        assert ctx.saga_name == "order-saga"

    def test_custom_headers(self) -> None:
        ctx = SagaContext(headers={"x-tenant": "acme"})
        assert ctx.headers == {"x-tenant": "acme"}


class TestSagaContextSetGetResult:
    def test_set_and_get_result(self) -> None:
        ctx = SagaContext()
        ctx.set_result("step-1", {"order_id": 42})
        assert ctx.get_result("step-1") == {"order_id": 42}

    def test_get_result_missing_returns_none(self) -> None:
        ctx = SagaContext()
        assert ctx.get_result("nonexistent") is None

    def test_overwrite_result(self) -> None:
        ctx = SagaContext()
        ctx.set_result("step-1", "first")
        ctx.set_result("step-1", "second")
        assert ctx.get_result("step-1") == "second"

    def test_multiple_step_results_isolated(self) -> None:
        ctx = SagaContext()
        ctx.set_result("a", 1)
        ctx.set_result("b", 2)
        assert ctx.get_result("a") == 1
        assert ctx.get_result("b") == 2


class TestSagaContextSetGetVariable:
    def test_set_and_get_variable(self) -> None:
        ctx = SagaContext()
        ctx.set_variable("user_id", "usr-99")
        assert ctx.get_variable("user_id") == "usr-99"

    def test_get_variable_missing_returns_none(self) -> None:
        ctx = SagaContext()
        assert ctx.get_variable("missing") is None

    def test_overwrite_variable(self) -> None:
        ctx = SagaContext()
        ctx.set_variable("key", "v1")
        ctx.set_variable("key", "v2")
        assert ctx.get_variable("key") == "v2"

    def test_multiple_variables_isolated(self) -> None:
        ctx = SagaContext()
        ctx.set_variable("x", 10)
        ctx.set_variable("y", 20)
        assert ctx.get_variable("x") == 10
        assert ctx.get_variable("y") == 20


class TestSagaContextSetStepStatus:
    def test_set_step_status_pending(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("step-1", StepStatus.PENDING)
        assert ctx.step_statuses["step-1"] == StepStatus.PENDING

    def test_set_step_status_done(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("step-1", StepStatus.DONE)
        assert ctx.step_statuses["step-1"] == StepStatus.DONE

    def test_set_step_status_failed(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("step-1", StepStatus.FAILED)
        assert ctx.step_statuses["step-1"] == StepStatus.FAILED

    def test_set_step_status_compensated(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("step-1", StepStatus.COMPENSATED)
        assert ctx.step_statuses["step-1"] == StepStatus.COMPENSATED

    def test_update_step_status(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("step-1", StepStatus.RUNNING)
        ctx.set_step_status("step-1", StepStatus.DONE)
        assert ctx.step_statuses["step-1"] == StepStatus.DONE

    def test_multiple_steps_tracked(self) -> None:
        ctx = SagaContext()
        ctx.set_step_status("a", StepStatus.DONE)
        ctx.set_step_status("b", StepStatus.FAILED)
        assert ctx.step_statuses["a"] == StepStatus.DONE
        assert ctx.step_statuses["b"] == StepStatus.FAILED


class TestSagaContextIdempotencyKeys:
    def test_key_absent_initially(self) -> None:
        ctx = SagaContext()
        assert not ctx.has_idempotency_key("k1")

    def test_add_and_check_key(self) -> None:
        ctx = SagaContext()
        ctx.add_idempotency_key("k1")
        assert ctx.has_idempotency_key("k1")

    def test_other_key_still_absent(self) -> None:
        ctx = SagaContext()
        ctx.add_idempotency_key("k1")
        assert not ctx.has_idempotency_key("k2")

    def test_add_same_key_twice_no_error(self) -> None:
        ctx = SagaContext()
        ctx.add_idempotency_key("k1")
        ctx.add_idempotency_key("k1")
        assert ctx.has_idempotency_key("k1")
        assert len(ctx.idempotency_keys) == 1

    def test_multiple_keys(self) -> None:
        ctx = SagaContext()
        ctx.add_idempotency_key("k1")
        ctx.add_idempotency_key("k2")
        assert ctx.has_idempotency_key("k1")
        assert ctx.has_idempotency_key("k2")


# ── StepOutcome ───────────────────────────────────────────────


class TestStepOutcome:
    def test_fields_stored_correctly(self) -> None:
        started = _now()
        outcome = StepOutcome(
            status=StepStatus.DONE,
            attempts=2,
            latency_ms=50.0,
            result="payload",
            error=None,
            compensated=False,
            started_at=started,
            compensation_result=None,
            compensation_error=None,
        )
        assert outcome.status == StepStatus.DONE
        assert outcome.attempts == 2
        assert outcome.latency_ms == 50.0
        assert outcome.result == "payload"
        assert outcome.error is None
        assert outcome.compensated is False
        assert outcome.started_at == started
        assert outcome.compensation_result is None
        assert outcome.compensation_error is None

    def test_frozen_raises_on_status_mutation(self) -> None:
        outcome = _make_step_outcome()
        with pytest.raises(AttributeError):
            outcome.status = StepStatus.FAILED  # type: ignore[misc]

    def test_frozen_raises_on_result_mutation(self) -> None:
        outcome = _make_step_outcome()
        with pytest.raises(AttributeError):
            outcome.result = "new"  # type: ignore[misc]

    def test_frozen_raises_on_attempts_mutation(self) -> None:
        outcome = _make_step_outcome()
        with pytest.raises(AttributeError):
            outcome.attempts = 99  # type: ignore[misc]

    def test_frozen_raises_on_latency_mutation(self) -> None:
        outcome = _make_step_outcome()
        with pytest.raises(AttributeError):
            outcome.latency_ms = 0.0  # type: ignore[misc]

    def test_failed_outcome_stores_error(self) -> None:
        err = ValueError("step failed")
        outcome = _make_step_outcome(status=StepStatus.FAILED, error=err)
        assert outcome.error is err
        assert outcome.status == StepStatus.FAILED

    def test_compensated_outcome_stores_compensation_result(self) -> None:
        outcome = _make_step_outcome(
            status=StepStatus.COMPENSATED,
            compensated=True,
            compensation_result="rolled-back",
        )
        assert outcome.compensated is True
        assert outcome.compensation_result == "rolled-back"


# ── SagaResult ────────────────────────────────────────────────


class TestSagaResult:
    def _build(
        self,
        steps: dict[str, StepOutcome] | None = None,
        success: bool = True,
        error: Exception | None = None,
    ) -> SagaResult:
        now = _now()
        return SagaResult(
            saga_name="order-saga",
            correlation_id="corr-1",
            started_at=now,
            completed_at=now,
            success=success,
            error=error,
            headers={"x-tenant": "acme"},
            steps=steps or {},
        )

    def test_result_of_existing_step(self) -> None:
        outcome = _make_step_outcome(result={"id": 1})
        result = self._build(steps={"step-1": outcome})
        assert result.result_of("step-1") == {"id": 1}

    def test_result_of_missing_step_returns_none(self) -> None:
        result = self._build()
        assert result.result_of("nonexistent") is None

    def test_failed_steps_filters_failed(self) -> None:
        done = _make_step_outcome(status=StepStatus.DONE)
        failed = _make_step_outcome(status=StepStatus.FAILED, error=RuntimeError("boom"))
        compensated = _make_step_outcome(status=StepStatus.COMPENSATED, compensated=True)
        result = self._build(steps={"s1": done, "s2": failed, "s3": compensated})
        assert result.failed_steps() == {"s2": failed}

    def test_failed_steps_empty_when_all_done(self) -> None:
        done1 = _make_step_outcome(status=StepStatus.DONE)
        done2 = _make_step_outcome(status=StepStatus.DONE)
        result = self._build(steps={"s1": done1, "s2": done2})
        assert result.failed_steps() == {}

    def test_compensated_steps_filters_compensated(self) -> None:
        done = _make_step_outcome(status=StepStatus.DONE)
        failed = _make_step_outcome(status=StepStatus.FAILED, error=RuntimeError("boom"))
        compensated = _make_step_outcome(status=StepStatus.COMPENSATED, compensated=True)
        result = self._build(steps={"s1": done, "s2": failed, "s3": compensated})
        assert result.compensated_steps() == {"s3": compensated}

    def test_compensated_steps_empty_when_none_compensated(self) -> None:
        done = _make_step_outcome(status=StepStatus.DONE)
        result = self._build(steps={"s1": done})
        assert result.compensated_steps() == {}

    def test_multiple_failed_steps(self) -> None:
        f1 = _make_step_outcome(status=StepStatus.FAILED, error=ValueError("e1"))
        f2 = _make_step_outcome(status=StepStatus.FAILED, error=ValueError("e2"))
        result = self._build(steps={"a": f1, "b": f2})
        failed = result.failed_steps()
        assert set(failed.keys()) == {"a", "b"}

    def test_multiple_compensated_steps(self) -> None:
        c1 = _make_step_outcome(status=StepStatus.COMPENSATED, compensated=True)
        c2 = _make_step_outcome(status=StepStatus.COMPENSATED, compensated=True)
        result = self._build(steps={"a": c1, "b": c2})
        compensated = result.compensated_steps()
        assert set(compensated.keys()) == {"a", "b"}

    def test_frozen_raises_on_mutation(self) -> None:
        result = self._build()
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]

    def test_success_field_stored(self) -> None:
        result = self._build(success=True)
        assert result.success is True

    def test_error_field_stored(self) -> None:
        err = RuntimeError("saga failed")
        result = self._build(success=False, error=err)
        assert result.error is err


# ── FailureReport ─────────────────────────────────────────────


class TestFailureReport:
    def test_basic_fields_stored(self) -> None:
        err = ValueError("step blew up")
        report = FailureReport(
            saga_name="order-saga",
            correlation_id="corr-1",
            failed_step_id="step-pay",
            error=err,
        )
        assert report.saga_name == "order-saga"
        assert report.correlation_id == "corr-1"
        assert report.failed_step_id == "step-pay"
        assert report.error is err

    def test_completed_steps_default_empty(self) -> None:
        report = FailureReport(
            saga_name="s",
            correlation_id="c",
            failed_step_id="x",
            error=RuntimeError(),
        )
        assert report.completed_steps == []

    def test_compensated_steps_default_empty(self) -> None:
        report = FailureReport(
            saga_name="s",
            correlation_id="c",
            failed_step_id="x",
            error=RuntimeError(),
        )
        assert report.compensated_steps == []

    def test_compensation_errors_default_empty(self) -> None:
        report = FailureReport(
            saga_name="s",
            correlation_id="c",
            failed_step_id="x",
            error=RuntimeError(),
        )
        assert report.compensation_errors == {}

    def test_custom_completed_and_compensated(self) -> None:
        comp_err = OSError("io fail")
        report = FailureReport(
            saga_name="order-saga",
            correlation_id="corr-2",
            failed_step_id="step-ship",
            error=ValueError("ship failed"),
            completed_steps=["step-pay", "step-reserve"],
            compensated_steps=["step-pay"],
            compensation_errors={"step-reserve": comp_err},
        )
        assert report.completed_steps == ["step-pay", "step-reserve"]
        assert report.compensated_steps == ["step-pay"]
        assert report.compensation_errors == {"step-reserve": comp_err}

    def test_frozen_raises_on_mutation(self) -> None:
        report = FailureReport(
            saga_name="s",
            correlation_id="c",
            failed_step_id="x",
            error=RuntimeError(),
        )
        with pytest.raises(AttributeError):
            report.saga_name = "new"  # type: ignore[misc]
