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
"""Shared foundations of the transactional engine — used by saga, workflow and TCC."""

from __future__ import annotations

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
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.dlq import DeadLetterEntry, DeadLetterService, DeadLetterStore
from pyfly.transactional.core.event_gateway import EventGateway, EventTrigger
from pyfly.transactional.core.events import (
    CompositeOrchestrationEvents,
    LoggerOrchestrationEvents,
    OrchestrationEvents,
)
from pyfly.transactional.core.exceptions import (
    OrchestrationError,
    OrchestrationValidationError,
    StepFailedError,
    StepTimeoutError,
)
from pyfly.transactional.core.metrics import OrchestrationMetrics
from pyfly.transactional.core.model import (
    CompensationPolicy,
    ExecutionPattern,
    ExecutionStatus,
    RetryPolicy,
    StepStatus,
    TccPhase,
    TriggerMode,
)
from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    ExecutionState,
    InMemoryPersistenceProvider,
    StateSerializer,
)
from pyfly.transactional.core.recovery import RecoveryService
from pyfly.transactional.core.report import (
    CompensationReport,
    ExecutionReport,
    ExecutionReportBuilder,
    StepReport,
)
from pyfly.transactional.core.scheduling import OrchestrationScheduler, ScheduledTask
from pyfly.transactional.core.step_invoker import StepInvoker
from pyfly.transactional.core.topology import TopologyBuilder, TopologyError
from pyfly.transactional.core.tracer import OrchestrationTracer
from pyfly.transactional.core.validator import OrchestrationValidator, ValidationIssue

__all__ = [
    "CompensationError",
    "CompensationPolicy",
    "CompensationReport",
    "CompositeOrchestrationEvents",
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
    "LoggerOrchestrationEvents",
    "OrchestrationError",
    "OrchestrationEvents",
    "OrchestrationMetrics",
    "OrchestrationScheduler",
    "OrchestrationTracer",
    "OrchestrationValidationError",
    "OrchestrationValidator",
    "RecoveryService",
    "Required",
    "RetryPolicy",
    "ScheduledTask",
    "SetVariable",
    "StateSerializer",
    "StepFailedError",
    "StepInvoker",
    "StepReport",
    "StepStatus",
    "StepTimeoutError",
    "TccPhase",
    "TopologyBuilder",
    "TopologyError",
    "TriggerMode",
    "ValidationIssue",
    "Variable",
    "Variables",
]
