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
"""PyFly transactional engine — saga, workflow and TCC orchestration.

Public API mirrors ``org.fireflyframework.orchestration``:

* **Saga**: ``@saga``, ``@saga_step``, ``SagaEngine`` — sequential, compensating.
* **Workflow**: ``@workflow``, ``@workflow_step``, ``@wait_for_signal``,
  ``@wait_for_timer``, ``@child_workflow`` — long-running, signal-driven, DAG.
* **TCC**: ``@tcc``, ``@tcc_participant``, ``@try_method``, ``@confirm_method``,
  ``@cancel_method`` — Try-Confirm-Cancel two-phase commit.

Plus shared infrastructure: persistence (in-memory, Redis, SQLAlchemy, cache),
DLQ, recovery, scheduling, validator, REST controllers, observability.
"""

from __future__ import annotations

from pyfly.transactional.builder import OrchestrationBuilder
from pyfly.transactional.core import (
    CompensationPolicy,
    CompositeOrchestrationEvents,
    DeadLetterEntry,
    DeadLetterService,
    DeadLetterStore,
    EventGateway,
    EventTrigger,
    ExecutionContext,
    ExecutionPattern,
    ExecutionPersistenceProvider,
    ExecutionReport,
    ExecutionReportBuilder,
    ExecutionState,
    ExecutionStatus,
    InMemoryPersistenceProvider,
    LoggerOrchestrationEvents,
    OrchestrationEvents,
    OrchestrationMetrics,
    OrchestrationScheduler,
    OrchestrationTracer,
    OrchestrationValidator,
    RecoveryService,
    RetryPolicy,
    ScheduledTask,
    StateSerializer,
    StepInvoker,
    StepReport,
    StepStatus,
    TccPhase,
    TopologyBuilder,
    TriggerMode,
    ValidationIssue,
)
from pyfly.transactional.core.argument import (
    CompensationError,
    CorrelationId,
    FromCompensationResult,
    FromStep,
    Header,
    Headers,
    Input,
    Required,
    SetVariable,
    Variable,
    Variables,
)
from pyfly.transactional.decorators import enable_transactional_engine
from pyfly.transactional.health import OrchestrationHealthIndicator
from pyfly.transactional.shared.types import (
    BackpressureConfig,
)
from pyfly.transactional.shared.types import (
    CompensationPolicy as LegacyCompensationPolicy,
)
from pyfly.transactional.shared.types import (
    StepStatus as LegacyStepStatus,
)
from pyfly.transactional.workflow import (
    ChildWorkflow,
    ChildWorkflowService,
    CompensationStep,
    ContinueAsNewService,
    OnStepComplete,
    OnWorkflowComplete,
    OnWorkflowError,
    ScheduledWorkflow,
    SignalService,
    TimerService,
    WaitForAll,
    WaitForAny,
    WaitForSignal,
    WaitForTimer,
    Workflow,
    WorkflowBuilder,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowExecutor,
    WorkflowQuery,
    WorkflowQueryService,
    WorkflowRegistry,
    WorkflowResult,
    WorkflowStep,
    WorkflowStepDefinition,
    child_workflow,
    compensation_step,
    on_step_complete,
    on_workflow_complete,
    on_workflow_error,
    scheduled_workflow,
    wait_for_all,
    wait_for_any,
    wait_for_signal,
    wait_for_timer,
    workflow,
    workflow_query,
    workflow_step,
)

__all__ = [
    "BackpressureConfig",
    "ChildWorkflow",
    "ChildWorkflowService",
    "CompensationError",
    "CompensationPolicy",
    "CompensationStep",
    "CompositeOrchestrationEvents",
    "ContinueAsNewService",
    "CorrelationId",
    "DeadLetterEntry",
    "DeadLetterService",
    "DeadLetterStore",
    "EventGateway",
    "EventTrigger",
    "ExecutionContext",
    "ExecutionPattern",
    "ExecutionPersistenceProvider",
    "ExecutionReport",
    "ExecutionReportBuilder",
    "ExecutionState",
    "ExecutionStatus",
    "FromCompensationResult",
    "FromStep",
    "Header",
    "Headers",
    "InMemoryPersistenceProvider",
    "Input",
    "LegacyCompensationPolicy",
    "LegacyStepStatus",
    "LoggerOrchestrationEvents",
    "OnStepComplete",
    "OnWorkflowComplete",
    "OnWorkflowError",
    "OrchestrationBuilder",
    "OrchestrationEvents",
    "OrchestrationHealthIndicator",
    "OrchestrationMetrics",
    "OrchestrationScheduler",
    "OrchestrationTracer",
    "OrchestrationValidator",
    "RecoveryService",
    "Required",
    "RetryPolicy",
    "ScheduledTask",
    "ScheduledWorkflow",
    "SetVariable",
    "SignalService",
    "StateSerializer",
    "StepInvoker",
    "StepReport",
    "StepStatus",
    "TccPhase",
    "TimerService",
    "TopologyBuilder",
    "TriggerMode",
    "ValidationIssue",
    "Variable",
    "Variables",
    "WaitForAll",
    "WaitForAny",
    "WaitForSignal",
    "WaitForTimer",
    "Workflow",
    "WorkflowBuilder",
    "WorkflowDefinition",
    "WorkflowEngine",
    "WorkflowExecutor",
    "WorkflowQuery",
    "WorkflowQueryService",
    "WorkflowRegistry",
    "WorkflowResult",
    "WorkflowStep",
    "WorkflowStepDefinition",
    "child_workflow",
    "compensation_step",
    "enable_transactional_engine",
    "on_step_complete",
    "on_workflow_complete",
    "on_workflow_error",
    "scheduled_workflow",
    "wait_for_all",
    "wait_for_any",
    "wait_for_signal",
    "wait_for_timer",
    "workflow",
    "workflow_query",
    "workflow_step",
]
