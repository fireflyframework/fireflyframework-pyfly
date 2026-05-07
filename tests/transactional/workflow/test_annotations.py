# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the workflow decorator family."""

from __future__ import annotations

from pyfly.transactional.workflow.annotations import (
    ChildWorkflow,
    OnStepComplete,
    OnWorkflowComplete,
    OnWorkflowError,
    WaitForAll,
    WaitForAny,
    WaitForSignal,
    WaitForTimer,
    Workflow,
    WorkflowQuery,
    WorkflowStep,
    child_workflow,
    on_step_complete,
    on_workflow_complete,
    on_workflow_error,
    wait_for_all,
    wait_for_any,
    wait_for_signal,
    wait_for_timer,
    workflow,
    workflow_query,
    workflow_step,
)


def test_workflow_attaches_metadata() -> None:
    @workflow(id="orderProcess", description="orders")
    class Order: ...

    assert isinstance(Order.__pyfly_workflow__, Workflow)
    assert Order.__pyfly_workflow__.id == "orderProcess"


def test_workflow_step_attaches_metadata() -> None:
    @workflow_step(id="reserve", depends_on=["validate"], timeout_ms=5000)
    async def reserve() -> None: ...

    meta: WorkflowStep = reserve.__pyfly_workflow_step__  # type: ignore[attr-defined]
    assert meta.id == "reserve"
    assert meta.depends_on == ("validate",)
    assert meta.timeout_ms == 5000


def test_wait_for_signal() -> None:
    @wait_for_signal("approved", timeout_ms=10000)
    async def step() -> None: ...

    meta: WaitForSignal = step.__pyfly_workflow_wait_signal__  # type: ignore[attr-defined]
    assert meta.name == "approved"
    assert meta.timeout_ms == 10000


def test_wait_for_timer() -> None:
    @wait_for_timer(delay_ms=5000, timer_id="cool-down")
    async def step() -> None: ...

    meta: WaitForTimer = step.__pyfly_workflow_wait_timer__  # type: ignore[attr-defined]
    assert meta.delay_ms == 5000


def test_wait_for_all() -> None:
    @wait_for_all("sig-a", "sig-b", timeout_ms=2000)
    async def step() -> None: ...

    meta: WaitForAll = step.__pyfly_workflow_wait_all__  # type: ignore[attr-defined]
    assert meta.signals == ("sig-a", "sig-b")


def test_wait_for_any() -> None:
    @wait_for_any("a", "b")
    async def step() -> None: ...

    meta: WaitForAny = step.__pyfly_workflow_wait_any__  # type: ignore[attr-defined]
    assert "a" in meta.signals


def test_child_workflow() -> None:
    @child_workflow(workflow_id="inner", wait_for_completion=False, timeout_ms=1000)
    async def step() -> None: ...

    meta: ChildWorkflow = step.__pyfly_workflow_child__  # type: ignore[attr-defined]
    assert meta.workflow_id == "inner"
    assert meta.wait_for_completion is False


def test_workflow_query() -> None:
    @workflow_query()
    async def status() -> str:
        return "ok"

    meta: WorkflowQuery = status.__pyfly_workflow_query__  # type: ignore[attr-defined]
    assert meta.name == "status"


def test_lifecycle_decorators() -> None:
    @on_workflow_complete
    async def done() -> None: ...

    @on_workflow_error
    async def errored() -> None: ...

    @on_step_complete(step_id="reserve")
    async def step_done() -> None: ...

    assert isinstance(done.__pyfly_workflow_on_complete__, OnWorkflowComplete)  # type: ignore[attr-defined]
    assert isinstance(errored.__pyfly_workflow_on_error__, OnWorkflowError)  # type: ignore[attr-defined]
    assert isinstance(step_done.__pyfly_workflow_on_step__, OnStepComplete)  # type: ignore[attr-defined]
    assert step_done.__pyfly_workflow_on_step__.step_id == "reserve"  # type: ignore[attr-defined]
