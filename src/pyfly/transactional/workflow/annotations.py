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
"""Workflow decorators — class-level ``@workflow`` and method-level step / signal /
timer / child-workflow / compensation / lifecycle annotations.

Mirrors ``org.fireflyframework.orchestration.workflow.annotation`` from the
Java engine.  The decorators only attach metadata (``__pyfly_workflow_*__``)
to the class / function; the real runtime resolution lives in
:class:`WorkflowRegistry`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyfly.transactional.core.model import TriggerMode

# Re-export argument annotations under workflow.annotations so example code
# only needs ``from pyfly.transactional.workflow import ...``.
__all_argument_markers = [
    "CorrelationId",
    "FromStep",
    "Header",
    "Headers",
    "Input",
    "Required",
    "SetVariable",
    "Variable",
    "Variables",
]


# --- Class-level metadata containers ----------------------------------------


@dataclass(frozen=True)
class Workflow:
    """Marker dataclass attached to ``cls.__pyfly_workflow__``."""

    id: str
    name: str
    description: str = ""
    version: int = 1
    trigger_mode: TriggerMode = TriggerMode.SYNC
    trigger_event_type: str = ""
    timeout_ms: int = 0
    max_retries: int = 0
    retry_delay_ms: int = 0
    publish_events: bool = True
    layer_concurrency: int = 0


@dataclass(frozen=True)
class WorkflowStep:
    """Per-method workflow-step metadata."""

    id: str
    name: str = ""
    description: str = ""
    depends_on: tuple[str, ...] = ()
    output_event_type: str = ""
    timeout_ms: int = 0
    max_retries: int = 0
    retry_delay_ms: int = 0
    condition: str = ""
    async_: bool = False
    compensatable: bool = False
    compensation_method: str = ""


@dataclass(frozen=True)
class CompensationStep:
    """Marks a method as the compensation handler for *for_step*."""

    for_step: str


@dataclass(frozen=True)
class WaitForSignal:
    name: str
    timeout_ms: int = 0


@dataclass(frozen=True)
class WaitForTimer:
    delay_ms: int
    timer_id: str = ""


@dataclass(frozen=True)
class WaitForAll:
    signals: tuple[str, ...] = ()
    timeout_ms: int = 0
    timers: tuple[int, ...] = ()  # timer delays (ms) that participate in the gate


@dataclass(frozen=True)
class WaitForAny:
    signals: tuple[str, ...] = ()
    timeout_ms: int = 0
    timers: tuple[int, ...] = ()  # timer delays (ms) that participate in the gate


@dataclass(frozen=True)
class ChildWorkflow:
    workflow_id: str
    wait_for_completion: bool = True
    timeout_ms: int = 0


@dataclass(frozen=True)
class WorkflowQuery:
    name: str = ""


@dataclass(frozen=True)
class OnWorkflowComplete:
    pass


@dataclass(frozen=True)
class OnWorkflowError:
    # When suppress_error is True the workflow ends COMPLETED instead of FAILED.
    # error_types / step_ids optionally restrict which failures the handler
    # matches (by exception class name and failed step id).
    suppress_error: bool = False
    error_types: tuple[str, ...] = ()
    step_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OnStepComplete:
    step_id: str = ""


@dataclass(frozen=True)
class ScheduledWorkflow:
    cron: str = ""
    fixed_rate_ms: int | None = None
    fixed_delay_ms: int | None = None
    initial_delay_ms: int = 0
    enabled: bool = True
    description: str = ""


# --- Decorators ---------------------------------------------------------------


def workflow(
    *,
    id: str,
    name: str | None = None,
    description: str = "",
    version: int = 1,
    trigger_mode: TriggerMode = TriggerMode.SYNC,
    trigger_event_type: str = "",
    timeout_ms: int = 0,
    max_retries: int = 0,
    retry_delay_ms: int = 0,
    publish_events: bool = True,
    layer_concurrency: int = 0,
) -> Callable[[type], type]:
    """Mark a class as a workflow.  ``id`` doubles as the registry key."""

    def decorator(cls: type) -> type:
        cls.__pyfly_workflow__ = Workflow(  # type: ignore[attr-defined]
            id=id,
            name=name or id,
            description=description,
            version=version,
            trigger_mode=trigger_mode,
            trigger_event_type=trigger_event_type,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            retry_delay_ms=retry_delay_ms,
            publish_events=publish_events,
            layer_concurrency=layer_concurrency,
        )
        return cls

    return decorator


def workflow_step(
    *,
    id: str,
    name: str = "",
    description: str = "",
    depends_on: tuple[str, ...] | list[str] = (),
    output_event_type: str = "",
    timeout_ms: int = 0,
    max_retries: int = 0,
    retry_delay_ms: int = 0,
    condition: str = "",
    async_: bool = False,
    compensatable: bool = False,
    compensation_method: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method as a workflow step."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_step__ = WorkflowStep(  # type: ignore[attr-defined]
            id=id,
            name=name,
            description=description,
            depends_on=tuple(depends_on),
            output_event_type=output_event_type,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            retry_delay_ms=retry_delay_ms,
            condition=condition,
            async_=async_,
            compensatable=compensatable,
            compensation_method=compensation_method,
        )
        return fn

    return decorator


def compensation_step(*, for_step: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_compensation__ = CompensationStep(for_step=for_step)  # type: ignore[attr-defined]
        return fn

    return decorator


def wait_for_signal(name: str, *, timeout_ms: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_wait_signal__ = WaitForSignal(name=name, timeout_ms=timeout_ms)  # type: ignore[attr-defined]
        return fn

    return decorator


def wait_for_timer(*, delay_ms: int, timer_id: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_wait_timer__ = WaitForTimer(delay_ms=delay_ms, timer_id=timer_id)  # type: ignore[attr-defined]
        return fn

    return decorator


def wait_for_all(
    *signals: str, timeout_ms: int = 0, timers: tuple[int, ...] = ()
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_wait_all__ = WaitForAll(  # type: ignore[attr-defined]
            signals=signals, timeout_ms=timeout_ms, timers=tuple(timers)
        )
        return fn

    return decorator


def wait_for_any(
    *signals: str, timeout_ms: int = 0, timers: tuple[int, ...] = ()
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_wait_any__ = WaitForAny(  # type: ignore[attr-defined]
            signals=signals, timeout_ms=timeout_ms, timers=tuple(timers)
        )
        return fn

    return decorator


def child_workflow(
    *, workflow_id: str, wait_for_completion: bool = True, timeout_ms: int = 0
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_child__ = ChildWorkflow(  # type: ignore[attr-defined]
            workflow_id=workflow_id,
            wait_for_completion=wait_for_completion,
            timeout_ms=timeout_ms,
        )
        return fn

    return decorator


def workflow_query(*, name: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_query__ = WorkflowQuery(name=name or fn.__name__)  # type: ignore[attr-defined]
        return fn

    return decorator


def on_workflow_complete(fn: Callable[..., Any]) -> Callable[..., Any]:
    fn.__pyfly_workflow_on_complete__ = OnWorkflowComplete()  # type: ignore[attr-defined]
    return fn


def on_workflow_error(
    fn: Callable[..., Any] | None = None,
    *,
    suppress_error: bool = False,
    error_types: tuple[str, ...] = (),
    step_ids: tuple[str, ...] = (),
) -> Any:
    """Mark a workflow error callback.

    Usable bare (``@on_workflow_error``) or with options
    (``@on_workflow_error(suppress_error=True, error_types=("ValueError",))``).
    """

    def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
        target.__pyfly_workflow_on_error__ = OnWorkflowError(  # type: ignore[attr-defined]
            suppress_error=suppress_error,
            error_types=tuple(error_types),
            step_ids=tuple(step_ids),
        )
        return target

    if fn is not None:
        return decorator(fn)
    return decorator


def on_step_complete(*, step_id: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__pyfly_workflow_on_step__ = OnStepComplete(step_id=step_id)  # type: ignore[attr-defined]
        return fn

    return decorator


def scheduled_workflow(
    *,
    cron: str = "",
    fixed_rate_ms: int | None = None,
    fixed_delay_ms: int | None = None,
    initial_delay_ms: int = 0,
    enabled: bool = True,
    description: str = "",
) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        existing = list(getattr(cls, "__pyfly_workflow_scheduled__", []))
        existing.append(
            ScheduledWorkflow(
                cron=cron,
                fixed_rate_ms=fixed_rate_ms,
                fixed_delay_ms=fixed_delay_ms,
                initial_delay_ms=initial_delay_ms,
                enabled=enabled,
                description=description,
            )
        )
        cls.__pyfly_workflow_scheduled__ = existing  # type: ignore[attr-defined]
        return cls

    return decorator
