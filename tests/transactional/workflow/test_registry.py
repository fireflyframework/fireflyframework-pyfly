# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the WorkflowRegistry."""

from __future__ import annotations

import pytest

from pyfly.transactional.core.exceptions import OrchestrationValidationError
from pyfly.transactional.workflow.annotations import workflow, workflow_step
from pyfly.transactional.workflow.registry import WorkflowRegistry


def test_registers_workflow() -> None:
    @workflow(id="a")
    class A:
        @workflow_step(id="s1")
        async def s1(self) -> None: ...

    reg = WorkflowRegistry()
    definition = reg.register_from_bean(A())
    assert definition.id == "a"
    assert "s1" in definition.steps
    assert reg.get("a") is definition


def test_wait_for_all_any_timeouts_propagated() -> None:
    """Regression: @wait_for_all/@wait_for_any timeout_ms must reach the step
    definition (previously dropped, causing unbounded waits)."""
    from pyfly.transactional.workflow.annotations import wait_for_all, wait_for_any

    @workflow(id="waits")
    class W:
        @workflow_step(id="all")
        @wait_for_all("a", "b", timeout_ms=1500)
        async def all_step(self) -> None: ...

        @workflow_step(id="any")
        @wait_for_any("x", "y", timeout_ms=2500)
        async def any_step(self) -> None: ...

    definition = WorkflowRegistry().register_from_bean(W())
    assert definition.steps["all"].wait_for_all == ("a", "b")
    assert definition.steps["all"].wait_for_all_timeout_ms == 1500
    assert definition.steps["any"].wait_for_any == ("x", "y")
    assert definition.steps["any"].wait_for_any_timeout_ms == 2500


def test_unregistered_class_raises() -> None:
    class NotADecorated: ...

    reg = WorkflowRegistry()
    with pytest.raises(OrchestrationValidationError):
        reg.register_from_bean(NotADecorated())


def test_registers_compensation_method() -> None:
    @workflow(id="cb")
    class CB:
        @workflow_step(id="reserve", compensation_method="release", compensatable=True)
        async def reserve(self) -> None: ...

        from pyfly.transactional.workflow.annotations import compensation_step

        @compensation_step(for_step="reserve")
        async def release(self) -> None: ...

    reg = WorkflowRegistry()
    definition = reg.register_from_bean(CB())
    step = definition.steps["reserve"]
    assert step.compensation_method is not None


def test_compensation_method_names_a_plain_method() -> None:
    """``@workflow_step(compensation_method="release")`` resolves the method by its name, with no
    ``@compensation_step`` (the name used to be looked up among step ids, so it never resolved)."""

    @workflow(id="by-name")
    class ByName:
        @workflow_step(id="reserve", compensation_method="release", compensatable=True)
        async def reserve(self) -> None: ...

        async def release(self) -> None: ...

    definition = WorkflowRegistry().register_from_bean(ByName())
    assert definition.steps["reserve"].compensation_method is ByName.release


def test_compensation_step_for_the_step_id_still_resolves() -> None:
    from pyfly.transactional.workflow.annotations import compensation_step

    @workflow(id="by-step")
    class ByStep:
        @workflow_step(id="reserve", compensatable=True)
        async def reserve(self) -> None: ...

        @compensation_step(for_step="reserve")
        async def undo(self) -> None: ...

    definition = WorkflowRegistry().register_from_bean(ByStep())
    assert definition.steps["reserve"].compensation_method is ByStep.undo


def test_dag_with_cycle_rejected() -> None:
    @workflow(id="bad")
    class Bad:
        @workflow_step(id="a", depends_on=["b"])
        async def a(self) -> None: ...

        @workflow_step(id="b", depends_on=["a"])
        async def b(self) -> None: ...

    reg = WorkflowRegistry()
    with pytest.raises(OrchestrationValidationError):
        reg.register_from_bean(Bad())
