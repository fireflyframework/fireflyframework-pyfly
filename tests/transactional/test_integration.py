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
"""End-to-end integration tests — full saga + TCC execution.

These tests wire up **all** real components (no mocks) and execute complete
saga and TCC flows, verifying results, compensation, parallelism, retry,
persistence, and observability events.

Note: Step methods are defined as synchronous functions because the
``@saga_step`` / ``@try_method`` decorators wrap the original function with
a synchronous ``functools.wraps`` wrapper.  The ``StepInvoker._call`` fast
path correctly handles synchronous callables.
"""

from __future__ import annotations

import logging
from typing import Annotated

import pytest

from pyfly.transactional.saga.annotations import (
    FromStep,
    Input,
    saga,
    saga_step,
)
from pyfly.transactional.saga.core.result import SagaResult
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.shared.observability.events import LoggerEventsAdapter
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter
from pyfly.transactional.shared.types import StepStatus
from pyfly.transactional.tcc.annotations import (
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.core.result import TccResult
from pyfly.transactional.tcc.engine.argument_resolver import TccArgumentResolver
from pyfly.transactional.tcc.engine.execution_orchestrator import (
    TccExecutionOrchestrator,
)
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

# ── Saga engine factory ──────────────────────────────────────


def _build_saga_engine(
    registry: SagaRegistry,
    *,
    persistence_port: InMemoryPersistenceAdapter | None = None,
    events_port: LoggerEventsAdapter | None = None,
) -> SagaEngine:
    """Assemble a full saga engine stack from real components."""
    resolver = ArgumentResolver()
    invoker = StepInvoker(resolver)
    orchestrator = SagaExecutionOrchestrator(invoker, events_port=events_port)
    compensator = SagaCompensator(invoker, events_port=events_port)
    return SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=orchestrator,
        compensator=compensator,
        persistence_port=persistence_port,
        events_port=events_port,
    )


# ── TCC engine factory ───────────────────────────────────────


def _build_tcc_engine(
    registry: TccRegistry,
    *,
    persistence_port: InMemoryPersistenceAdapter | None = None,
    events_port: LoggerEventsAdapter | None = None,
) -> TccEngine:
    """Assemble a full TCC engine stack from real components."""
    resolver = TccArgumentResolver()
    invoker = TccParticipantInvoker(resolver)
    orchestrator = TccExecutionOrchestrator(invoker)
    return TccEngine(
        registry=registry,
        participant_invoker=invoker,
        orchestrator=orchestrator,
        persistence_port=persistence_port,
        events_port=events_port,
    )


# =====================================================================
# 1. Successful multi-step saga
# =====================================================================


class TestSuccessfulMultiStepSaga:
    """Verify a three-step saga (validate -> reserve -> charge) executes
    successfully and all step results are accessible in the SagaResult."""

    @pytest.mark.anyio
    async def test_three_step_saga_success(self) -> None:
        execution_log: list[str] = []

        @saga(name="order-saga")
        class OrderSaga:
            @saga_step(id="validate")
            def validate(self) -> str:
                execution_log.append("validate")
                return "valid"

            @saga_step(id="reserve", compensate="release", depends_on=["validate"])
            def reserve(self, result: Annotated[str, FromStep("validate")]) -> str:
                execution_log.append("reserve")
                return f"reserved-{result}"

            def release(self) -> None:
                execution_log.append("release")

            @saga_step(id="charge", depends_on=["reserve"])
            def charge(self, reserved: Annotated[str, FromStep("reserve")]) -> str:
                execution_log.append("charge")
                return f"charged-{reserved}"

        registry = SagaRegistry()
        registry.register_from_bean(OrderSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("order-saga")

        assert isinstance(result, SagaResult)
        assert result.success is True
        assert result.error is None
        assert result.saga_name == "order-saga"

        assert result.result_of("validate") == "valid"
        assert result.result_of("reserve") == "reserved-valid"
        assert result.result_of("charge") == "charged-reserved-valid"

        assert execution_log == ["validate", "reserve", "charge"]

    @pytest.mark.anyio
    async def test_all_step_outcomes_accessible(self) -> None:
        @saga(name="simple-saga")
        class SimpleSaga:
            @saga_step(id="a")
            def step_a(self) -> int:
                return 1

            @saga_step(id="b", depends_on=["a"])
            def step_b(self, prev: Annotated[int, FromStep("a")]) -> int:
                return prev + 10

            @saga_step(id="c", depends_on=["b"])
            def step_c(self, prev: Annotated[int, FromStep("b")]) -> int:
                return prev + 100

        registry = SagaRegistry()
        registry.register_from_bean(SimpleSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("simple-saga")

        assert result.success is True
        assert len(result.steps) == 3
        for step_id in ("a", "b", "c"):
            assert step_id in result.steps
            assert result.steps[step_id].status == StepStatus.DONE
            assert result.steps[step_id].attempts == 1

        assert result.result_of("a") == 1
        assert result.result_of("b") == 11
        assert result.result_of("c") == 111


# =====================================================================
# 2. Failed saga with compensation
# =====================================================================


class TestFailedSagaWithCompensation:
    """Verify that when a step fails, completed steps are compensated
    and the SagaResult reflects the failure."""

    @pytest.mark.anyio
    async def test_third_step_fails_triggers_compensation(self) -> None:
        compensation_log: list[str] = []

        @saga(name="fail-saga")
        class FailSaga:
            @saga_step(id="validate", compensate="undo_validate")
            def validate(self) -> str:
                return "valid"

            def undo_validate(self) -> None:
                compensation_log.append("undo_validate")

            @saga_step(id="reserve", compensate="release", depends_on=["validate"])
            def reserve(self, result: Annotated[str, FromStep("validate")]) -> str:
                return f"reserved-{result}"

            def release(self) -> None:
                compensation_log.append("release")

            @saga_step(id="charge", depends_on=["reserve"])
            def charge(self, reserved: Annotated[str, FromStep("reserve")]) -> str:
                raise RuntimeError("payment gateway timeout")

        registry = SagaRegistry()
        registry.register_from_bean(FailSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("fail-saga")

        assert result.success is False
        assert result.error is not None
        assert "payment gateway timeout" in str(result.error)

        # Compensation should have run for completed steps in reverse order.
        assert "release" in compensation_log
        assert "undo_validate" in compensation_log
        # Strict sequential: reverse order of completion.
        assert compensation_log == ["release", "undo_validate"]

    @pytest.mark.anyio
    async def test_failed_step_has_failed_status(self) -> None:
        @saga(name="fail-status-saga")
        class FailStatusSaga:
            @saga_step(id="step1")
            def step1(self) -> str:
                return "ok"

            @saga_step(id="step2", depends_on=["step1"])
            def step2(self, prev: Annotated[str, FromStep("step1")]) -> str:
                raise ValueError("boom")

        registry = SagaRegistry()
        registry.register_from_bean(FailStatusSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("fail-status-saga")

        assert result.success is False
        assert result.steps["step1"].status == StepStatus.DONE
        assert result.steps["step2"].status == StepStatus.FAILED


# =====================================================================
# 3. Saga with parallel steps
# =====================================================================


class TestSagaWithParallelSteps:
    """Verify that independent steps run in the same topology layer
    (parallel execution) and dependent steps wait for their results."""

    @pytest.mark.anyio
    async def test_parallel_steps_and_dependent_step(self) -> None:
        execution_log: list[str] = []

        @saga(name="parallel-saga")
        class ParallelSaga:
            @saga_step(id="fetch_user")
            def fetch_user(self) -> dict[str, str]:
                execution_log.append("fetch_user")
                return {"name": "Alice"}

            @saga_step(id="fetch_inventory")
            def fetch_inventory(self) -> dict[str, int]:
                execution_log.append("fetch_inventory")
                return {"stock": 42}

            @saga_step(
                id="process_order",
                depends_on=["fetch_user", "fetch_inventory"],
            )
            def process_order(
                self,
                user: Annotated[dict[str, str], FromStep("fetch_user")],
                inventory: Annotated[dict[str, int], FromStep("fetch_inventory")],
            ) -> str:
                execution_log.append("process_order")
                return f"order for {user['name']} with stock {inventory['stock']}"

        registry = SagaRegistry()
        registry.register_from_bean(ParallelSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("parallel-saga")

        assert result.success is True

        # Both parallel steps completed before the dependent one.
        assert "fetch_user" in execution_log
        assert "fetch_inventory" in execution_log
        assert execution_log[-1] == "process_order"

        # Dependent step received results from both parents.
        assert result.result_of("process_order") == ("order for Alice with stock 42")
        assert result.result_of("fetch_user") == {"name": "Alice"}
        assert result.result_of("fetch_inventory") == {"stock": 42}


# =====================================================================
# 4. Saga with retry
# =====================================================================


class TestSagaWithRetry:
    """Verify that a step configured with retry > 1 retries on failure
    and eventually succeeds."""

    @pytest.mark.anyio
    async def test_step_retries_then_succeeds(self) -> None:
        attempt_counter: dict[str, int] = {"count": 0}

        @saga(name="retry-saga")
        class RetrySaga:
            @saga_step(id="setup")
            def setup(self) -> str:
                return "ready"

            @saga_step(id="flaky", retry=3, depends_on=["setup"])
            def flaky(self, prev: Annotated[str, FromStep("setup")]) -> str:
                attempt_counter["count"] += 1
                if attempt_counter["count"] < 2:
                    raise RuntimeError("transient failure")
                return f"success-{prev}"

        registry = SagaRegistry()
        registry.register_from_bean(RetrySaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute("retry-saga")

        assert result.success is True
        assert result.result_of("flaky") == "success-ready"
        assert result.steps["flaky"].attempts > 1
        assert attempt_counter["count"] == 2


# =====================================================================
# 5. Successful TCC
# =====================================================================


class TestSuccessfulTcc:
    """Verify a TCC with two participants executes all three phases
    (try -> confirm) successfully and results are accessible."""

    @pytest.mark.anyio
    async def test_two_participant_tcc_success(self) -> None:
        execution_log: list[str] = []

        @tcc(name="payment-tcc")
        class PaymentTcc:
            @tcc_participant(id="reserve-credit", order=1)
            class ReserveCredit:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    execution_log.append("reserve-credit:try")
                    return "credit-reserved-100"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    execution_log.append("reserve-credit:confirm")

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    execution_log.append("reserve-credit:cancel")

            @tcc_participant(id="reserve-inventory", order=2)
            class ReserveInventory:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    execution_log.append("reserve-inventory:try")
                    return "inventory-reserved-5"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    execution_log.append("reserve-inventory:confirm")

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    execution_log.append("reserve-inventory:cancel")

        registry = TccRegistry()
        registry.register_from_bean(PaymentTcc())
        engine = _build_tcc_engine(registry)

        result = await engine.execute("payment-tcc")

        assert isinstance(result, TccResult)
        assert result.success is True
        assert result.error is None
        assert result.tcc_name == "payment-tcc"

        # Try results accessible.
        assert result.try_results["reserve-credit"] == "credit-reserved-100"
        assert result.try_results["reserve-inventory"] == "inventory-reserved-5"

        # result_of helper works.
        assert result.result_of("reserve-credit") == "credit-reserved-100"
        assert result.result_of("reserve-inventory") == "inventory-reserved-5"

        # Execution order: try phase then confirm phase.
        assert execution_log == [
            "reserve-credit:try",
            "reserve-inventory:try",
            "reserve-credit:confirm",
            "reserve-inventory:confirm",
        ]

    @pytest.mark.anyio
    async def test_tcc_final_phase_is_confirm_on_success(self) -> None:
        @tcc(name="confirm-phase-tcc")
        class ConfirmPhaseTcc:
            @tcc_participant(id="p1", order=1)
            class P1:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    return "tried"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    pass

        registry = TccRegistry()
        registry.register_from_bean(ConfirmPhaseTcc())
        engine = _build_tcc_engine(registry)

        result = await engine.execute("confirm-phase-tcc")

        assert result.success is True
        assert result.final_phase == TccPhase.CONFIRM


# =====================================================================
# 6. Failed TCC with cancel
# =====================================================================


class TestFailedTccWithCancel:
    """Verify that when a participant's try fails, previously successful
    participants are cancelled."""

    @pytest.mark.anyio
    async def test_second_participant_try_fails_triggers_cancel(self) -> None:
        execution_log: list[str] = []

        @tcc(name="cancel-tcc")
        class CancelTcc:
            @tcc_participant(id="p1", order=1)
            class P1:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    execution_log.append("p1:try")
                    return "p1-reserved"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    execution_log.append("p1:confirm")

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    execution_log.append("p1:cancel")

            @tcc_participant(id="p2", order=2)
            class P2:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    execution_log.append("p2:try")
                    raise RuntimeError("p2 try failed")

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    execution_log.append("p2:confirm")

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    execution_log.append("p2:cancel")

        registry = TccRegistry()
        registry.register_from_bean(CancelTcc())
        engine = _build_tcc_engine(registry)

        result = await engine.execute("cancel-tcc")

        assert result.success is False
        assert result.failed_participant_id == "p2"

        # P1 tried and was cancelled; P2 tried but failed.
        assert "p1:try" in execution_log
        assert "p2:try" in execution_log
        assert "p1:cancel" in execution_log

        # Confirm should NOT have run for either.
        assert "p1:confirm" not in execution_log
        assert "p2:confirm" not in execution_log

        # Cancel should NOT have run for P2 (it failed during try).
        assert "p2:cancel" not in execution_log

    @pytest.mark.anyio
    async def test_failed_tcc_final_phase_is_cancel(self) -> None:
        @tcc(name="cancel-phase-tcc")
        class CancelPhaseTcc:
            @tcc_participant(id="p1", order=1)
            class P1:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    return "ok"

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    pass

            @tcc_participant(id="p2", order=2)
            class P2:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    raise RuntimeError("fail")

                @cancel_method()
                def do_cancel(self, ctx: TccContext) -> None:
                    pass

        registry = TccRegistry()
        registry.register_from_bean(CancelPhaseTcc())
        engine = _build_tcc_engine(registry)

        result = await engine.execute("cancel-phase-tcc")

        assert result.success is False
        assert result.final_phase == TccPhase.CANCEL


# =====================================================================
# 7. Saga with events and persistence
# =====================================================================


class TestSagaWithEventsAndPersistence:
    """Verify that the saga engine integrates with the in-memory
    persistence adapter and the logger events adapter."""

    @pytest.mark.anyio
    async def test_persistence_records_state(self) -> None:
        @saga(name="persist-saga")
        class PersistSaga:
            @saga_step(id="work")
            def work(self) -> str:
                return "done"

        persistence = InMemoryPersistenceAdapter()
        registry = SagaRegistry()
        registry.register_from_bean(PersistSaga())
        engine = _build_saga_engine(registry, persistence_port=persistence)

        result = await engine.execute("persist-saga")

        assert result.success is True

        # Persistence should have the final state.
        state = await persistence.get_state(result.correlation_id)
        assert state is not None
        assert state["saga_name"] == "persist-saga"
        assert state["correlation_id"] == result.correlation_id
        assert state["status"] == "COMPLETED"
        assert state["successful"] is True

    @pytest.mark.anyio
    async def test_persistence_records_failed_state(self) -> None:
        @saga(name="persist-fail-saga")
        class PersistFailSaga:
            @saga_step(id="work")
            def work(self) -> str:
                raise RuntimeError("kaboom")

        persistence = InMemoryPersistenceAdapter()
        registry = SagaRegistry()
        registry.register_from_bean(PersistFailSaga())
        engine = _build_saga_engine(registry, persistence_port=persistence)

        result = await engine.execute("persist-fail-saga")

        assert result.success is False

        state = await persistence.get_state(result.correlation_id)
        assert state is not None
        assert state["status"] == "FAILED"
        assert state["successful"] is False

    @pytest.mark.anyio
    async def test_events_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        @saga(name="events-saga")
        class EventsSaga:
            @saga_step(id="step1")
            def step1(self) -> str:
                return "ok"

            @saga_step(id="step2", depends_on=["step1"])
            def step2(self, prev: Annotated[str, FromStep("step1")]) -> str:
                return f"also-{prev}"

        events_adapter = LoggerEventsAdapter()
        registry = SagaRegistry()
        registry.register_from_bean(EventsSaga())
        engine = _build_saga_engine(registry, events_port=events_adapter)

        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            result = await engine.execute("events-saga")

        assert result.success is True

        log_text = caplog.text

        # on_start event
        assert "events-saga" in log_text
        assert "started" in log_text

        # on_step_success events
        assert "step1" in log_text
        assert "step2" in log_text
        assert "succeeded" in log_text

        # on_completed event
        assert "completed" in log_text

    @pytest.mark.anyio
    async def test_events_and_persistence_together(self, caplog: pytest.LogCaptureFixture) -> None:
        @saga(name="full-saga")
        class FullSaga:
            @saga_step(id="alpha")
            def alpha(self) -> str:
                return "a"

            @saga_step(id="beta", depends_on=["alpha"])
            def beta(self, prev: Annotated[str, FromStep("alpha")]) -> str:
                return f"b-{prev}"

        persistence = InMemoryPersistenceAdapter()
        events_adapter = LoggerEventsAdapter()
        registry = SagaRegistry()
        registry.register_from_bean(FullSaga())
        engine = _build_saga_engine(
            registry,
            persistence_port=persistence,
            events_port=events_adapter,
        )

        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            result = await engine.execute("full-saga")

        assert result.success is True
        assert result.result_of("alpha") == "a"
        assert result.result_of("beta") == "b-a"

        # Persistence has state.
        state = await persistence.get_state(result.correlation_id)
        assert state is not None
        assert state["status"] == "COMPLETED"

        # Events were logged.
        assert "full-saga" in caplog.text
        assert "started" in caplog.text
        assert "completed" in caplog.text


# =====================================================================
# Additional edge-case integration tests
# =====================================================================


class TestSagaWithInputData:
    """Verify that input_data is propagated through the saga engine."""

    @pytest.mark.anyio
    async def test_input_data_accessible_in_steps(self) -> None:
        @saga(name="input-saga")
        class InputSaga:
            @saga_step(id="process")
            def process(self, data: Annotated[dict, Input()]) -> str:
                return f"processed-{data['order_id']}"

        registry = SagaRegistry()
        registry.register_from_bean(InputSaga())
        engine = _build_saga_engine(registry)

        result = await engine.execute(
            "input-saga",
            input_data={"order_id": "ORD-42"},
        )

        assert result.success is True
        assert result.result_of("process") == "processed-ORD-42"


class TestTccWithPersistenceAndEvents:
    """Verify TCC integrates with persistence and events."""

    @pytest.mark.anyio
    async def test_tcc_persistence_records_state(self) -> None:
        @tcc(name="persist-tcc")
        class PersistTcc:
            @tcc_participant(id="p1", order=1)
            class P1:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    return "tried"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    pass

        persistence = InMemoryPersistenceAdapter()
        registry = TccRegistry()
        registry.register_from_bean(PersistTcc())
        engine = _build_tcc_engine(registry, persistence_port=persistence)

        result = await engine.execute("persist-tcc")

        assert result.success is True
        state = await persistence.get_state(result.correlation_id)
        assert state is not None
        assert state["status"] == "COMPLETED"
        assert state["successful"] is True

    @pytest.mark.anyio
    async def test_tcc_events_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        @tcc(name="events-tcc")
        class EventsTcc:
            @tcc_participant(id="p1", order=1)
            class P1:
                @try_method()
                def do_try(self, ctx: TccContext) -> str:
                    return "tried"

                @confirm_method()
                def do_confirm(self, ctx: TccContext) -> None:
                    pass

        events_adapter = LoggerEventsAdapter()
        registry = TccRegistry()
        registry.register_from_bean(EventsTcc())
        engine = _build_tcc_engine(registry, events_port=events_adapter)

        with caplog.at_level(logging.INFO, logger="pyfly.transactional.events"):
            result = await engine.execute("events-tcc")

        assert result.success is True
        assert "events-tcc" in caplog.text
        assert "started" in caplog.text
        assert "completed" in caplog.text
