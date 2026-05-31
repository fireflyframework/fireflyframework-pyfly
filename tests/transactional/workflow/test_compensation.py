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
"""Integration tests for workflow step compensation on failure."""

from __future__ import annotations

from typing import Annotated

import pytest

from pyfly.transactional.core.argument import CompensationError, FromStep
from pyfly.transactional.core.events import LoggerOrchestrationEvents
from pyfly.transactional.core.model import ExecutionStatus, StepStatus
from pyfly.transactional.core.persistence import InMemoryPersistenceProvider
from pyfly.transactional.workflow.annotations import (
    compensation_step,
    workflow,
    workflow_step,
)
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.executor import WorkflowExecutor
from pyfly.transactional.workflow.registry import WorkflowRegistry


def _make_engine() -> tuple[WorkflowEngine, WorkflowRegistry]:
    registry = WorkflowRegistry()
    engine = WorkflowEngine(
        registry=registry,
        executor=WorkflowExecutor(events=LoggerOrchestrationEvents()),
        persistence=InMemoryPersistenceProvider(),
    )
    return engine, registry


class TestWorkflowCompensation:
    @pytest.mark.asyncio
    async def test_completed_compensatable_step_is_rolled_back_on_later_failure(self) -> None:
        """An early compensatable step must be compensated when a later step fails."""
        side_effects: list[str] = []

        @workflow(id="comp-basic")
        class CompBasic:
            @workflow_step(id="reserve", compensatable=True, compensation_method="release")
            async def reserve(self) -> str:
                side_effects.append("reserve")
                return "reserved"

            @workflow_step(id="charge", depends_on=["reserve"])
            async def charge(self) -> None:
                side_effects.append("charge")
                msg = "payment declined"
                raise RuntimeError(msg)

            @compensation_step(for_step="reserve")
            async def release(self) -> None:
                side_effects.append("release")

        engine, registry = _make_engine()
        registry.register_from_bean(CompBasic())

        result = await engine.start("comp-basic")

        assert result.status == ExecutionStatus.FAILED
        assert not result.successful
        # The compensation method ran after the failure.
        assert side_effects == ["reserve", "charge", "release"]

    @pytest.mark.asyncio
    async def test_multiple_compensations_run_in_reverse_order(self) -> None:
        """Completed compensatable steps roll back newest-first."""
        order: list[str] = []

        @workflow(id="comp-order")
        class CompOrder:
            @workflow_step(id="a", compensatable=True, compensation_method="undo_a")
            async def a(self) -> str:
                return "a"

            @workflow_step(id="b", depends_on=["a"], compensatable=True, compensation_method="undo_b")
            async def b(self) -> str:
                return "b"

            @workflow_step(id="c", depends_on=["b"])
            async def c(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

            @compensation_step(for_step="a")
            async def undo_a(self) -> None:
                order.append("undo_a")

            @compensation_step(for_step="b")
            async def undo_b(self) -> None:
                order.append("undo_b")

        engine, registry = _make_engine()
        registry.register_from_bean(CompOrder())

        result = await engine.start("comp-order")

        assert result.status == ExecutionStatus.FAILED
        # b completed after a, so its compensation runs first (reverse order).
        assert order == ["undo_b", "undo_a"]

    @pytest.mark.asyncio
    async def test_non_compensatable_step_is_not_compensated(self) -> None:
        """A completed step without ``compensatable=True`` must be left untouched."""
        side_effects: list[str] = []

        @workflow(id="comp-skip")
        class CompSkip:
            @workflow_step(id="plain")
            async def plain(self) -> str:
                return "plain"

            @workflow_step(id="fail", depends_on=["plain"])
            async def fail(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

            @compensation_step(for_step="plain")
            async def undo_plain(self) -> None:
                side_effects.append("undo_plain")

        engine, registry = _make_engine()
        registry.register_from_bean(CompSkip())

        result = await engine.start("comp-skip")

        assert result.status == ExecutionStatus.FAILED
        # 'plain' is not marked compensatable -> its handler must not run.
        assert side_effects == []

    @pytest.mark.asyncio
    async def test_compensation_receives_triggering_error_and_step_result(self) -> None:
        """Compensation methods can inject the original step result and the error."""
        captured: dict[str, object] = {}

        @workflow(id="comp-args")
        class CompArgs:
            @workflow_step(id="reserve", compensatable=True, compensation_method="release")
            async def reserve(self) -> dict[str, str]:
                return {"reservation_id": "R-1"}

            @workflow_step(id="charge", depends_on=["reserve"])
            async def charge(self) -> None:
                msg = "declined"
                raise RuntimeError(msg)

            @compensation_step(for_step="reserve")
            async def release(
                self,
                reservation: Annotated[dict, FromStep("reserve")],
                error: Annotated[BaseException, CompensationError()],
            ) -> None:
                captured["reservation"] = reservation
                captured["error"] = error

        engine, registry = _make_engine()
        registry.register_from_bean(CompArgs())

        result = await engine.start("comp-args")

        assert result.status == ExecutionStatus.FAILED
        assert captured["reservation"] == {"reservation_id": "R-1"}
        assert isinstance(captured["error"], BaseException)

    @pytest.mark.asyncio
    async def test_no_compensation_when_workflow_succeeds(self) -> None:
        """Compensation must not run on the happy path."""
        side_effects: list[str] = []

        @workflow(id="comp-happy")
        class CompHappy:
            @workflow_step(id="reserve", compensatable=True, compensation_method="release")
            async def reserve(self) -> str:
                return "reserved"

            @workflow_step(id="charge", depends_on=["reserve"])
            async def charge(self) -> str:
                return "charged"

            @compensation_step(for_step="reserve")
            async def release(self) -> None:
                side_effects.append("release")

        engine, registry = _make_engine()
        registry.register_from_bean(CompHappy())

        result = await engine.start("comp-happy")

        assert result.status == ExecutionStatus.COMPLETED
        assert side_effects == []

    @pytest.mark.asyncio
    async def test_compensation_records_step_status_in_context(self) -> None:
        """A rolled-back step is marked COMPENSATED on the execution context."""
        recorded: dict[str, StepStatus] = {}

        @workflow(id="comp-status")
        class CompStatus:
            @workflow_step(id="reserve", compensatable=True, compensation_method="release")
            async def reserve(self) -> str:
                return "reserved"

            @workflow_step(id="charge", depends_on=["reserve"])
            async def charge(self) -> None:
                msg = "boom"
                raise RuntimeError(msg)

            @compensation_step(for_step="reserve")
            async def release(self) -> None: ...

            @workflow_step(id="probe", depends_on=["charge"])
            async def probe(self) -> None: ...  # never reached

        engine, registry = _make_engine()
        registry.register_from_bean(CompStatus())

        # Drive directly through the executor to inspect the context.
        from pyfly.transactional.core.context import ExecutionContext
        from pyfly.transactional.core.exceptions import StepFailedError
        from pyfly.transactional.core.model import ExecutionPattern

        definition = registry.get("comp-status")
        assert definition is not None
        ctx = ExecutionContext(name="comp-status", pattern=ExecutionPattern.WORKFLOW)
        executor = WorkflowExecutor(events=LoggerOrchestrationEvents())

        with pytest.raises(StepFailedError):
            await executor.execute(definition, ctx)

        reserve_rec = ctx.get_step("reserve")
        assert reserve_rec is not None
        recorded["reserve"] = reserve_rec.status
        assert recorded["reserve"] == StepStatus.COMPENSATED
