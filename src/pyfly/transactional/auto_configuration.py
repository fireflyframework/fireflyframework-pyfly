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
"""Transactional engine auto-configuration — wires everything into the DI container.

Wires the *full* orchestration stack: shared core (events, metrics, tracer,
DLQ, recovery, scheduler, validator, persistence), saga subsystem, workflow
subsystem, TCC subsystem, REST controllers, health indicator.
"""

from __future__ import annotations

import logging

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property

# core/
from pyfly.transactional.core.argument import ArgumentResolver as CoreArgumentResolver
from pyfly.transactional.core.dlq import DeadLetterService, InMemoryDeadLetterStore
from pyfly.transactional.core.event_gateway import EventGateway
from pyfly.transactional.core.events import (
    CompositeOrchestrationEvents,
    LoggerOrchestrationEvents,
)
from pyfly.transactional.core.metrics import OrchestrationMetrics
from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    InMemoryPersistenceProvider,
)
from pyfly.transactional.core.recovery import RecoveryService
from pyfly.transactional.core.scheduling import OrchestrationScheduler
from pyfly.transactional.core.step_invoker import StepInvoker as CoreStepInvoker
from pyfly.transactional.core.tracer import OrchestrationTracer
from pyfly.transactional.core.validator import OrchestrationValidator
from pyfly.transactional.health import OrchestrationHealthIndicator
from pyfly.transactional.post_processor import OrchestrationBeanPostProcessor
from pyfly.transactional.rest.controllers import (
    DeadLetterController,
    OrchestrationController,
    WorkflowController,
)

# saga/
from pyfly.transactional.saga.config.properties import (
    BackpressureProperties,
    SagaEngineProperties,
)
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.persistence.recovery import SagaRecoveryService
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry

# Shared (legacy adapters kept for back-compat).
from pyfly.transactional.shared.observability.events import LoggerEventsAdapter
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter

# tcc/
from pyfly.transactional.tcc.config.properties import TccEngineProperties
from pyfly.transactional.tcc.engine.argument_resolver import TccArgumentResolver
from pyfly.transactional.tcc.engine.execution_orchestrator import (
    TccExecutionOrchestrator,
)
from pyfly.transactional.tcc.engine.participant_invoker import TccParticipantInvoker
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

# workflow/
from pyfly.transactional.workflow.child_workflow_service import ChildWorkflowService
from pyfly.transactional.workflow.continue_as_new_service import ContinueAsNewService
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.executor import WorkflowExecutor
from pyfly.transactional.workflow.query_service import WorkflowQueryService
from pyfly.transactional.workflow.registry import WorkflowRegistry
from pyfly.transactional.workflow.signal_service import SignalService
from pyfly.transactional.workflow.timer_service import TimerService

_logger = logging.getLogger(__name__)


@auto_configuration
@conditional_on_property("pyfly.transactional.enabled", having_value="true")
class TransactionalEngineAutoConfiguration:
    """Wire the full orchestration engine into the DI container."""

    # -- Configuration properties -------------------------------------------

    @bean
    def saga_engine_properties(self) -> SagaEngineProperties:
        return SagaEngineProperties()

    @bean
    def tcc_engine_properties(self) -> TccEngineProperties:
        return TccEngineProperties()

    @bean
    def backpressure_properties(self) -> BackpressureProperties:
        return BackpressureProperties()

    # -- Core observability infrastructure ----------------------------------

    @bean
    def orchestration_metrics(self) -> OrchestrationMetrics:
        return OrchestrationMetrics()

    @bean
    def orchestration_tracer(self) -> OrchestrationTracer:
        return OrchestrationTracer()

    @bean
    def logger_orchestration_events(self) -> LoggerOrchestrationEvents:
        return LoggerOrchestrationEvents()

    @bean
    def composite_orchestration_events(
        self,
        logger_events: LoggerOrchestrationEvents,
        metrics: OrchestrationMetrics,
    ) -> CompositeOrchestrationEvents:
        composite = CompositeOrchestrationEvents()
        composite.add(logger_events)
        composite.add(metrics)
        return composite

    # -- Core persistence + DLQ + recovery + scheduler + validator ----------

    @bean
    def orchestration_persistence(self) -> ExecutionPersistenceProvider:
        return InMemoryPersistenceProvider()

    @bean
    def dead_letter_store(self) -> InMemoryDeadLetterStore:
        return InMemoryDeadLetterStore()

    @bean
    def dead_letter_service(self, store: InMemoryDeadLetterStore) -> DeadLetterService:
        return DeadLetterService(store=store)

    @bean
    def recovery_service(self, persistence: ExecutionPersistenceProvider) -> RecoveryService:
        return RecoveryService(persistence=persistence)

    @bean
    def orchestration_scheduler(self) -> OrchestrationScheduler:
        return OrchestrationScheduler()

    @bean
    def orchestration_validator(self) -> OrchestrationValidator:
        return OrchestrationValidator()

    @bean
    def event_gateway(self) -> EventGateway:
        return EventGateway()

    @bean
    def core_argument_resolver(self) -> CoreArgumentResolver:
        return CoreArgumentResolver()

    @bean
    def core_step_invoker(self, resolver: CoreArgumentResolver) -> CoreStepInvoker:
        return CoreStepInvoker(argument_resolver=resolver)

    # -- Legacy infrastructure adapters (kept for back-compat) --------------

    @bean
    def in_memory_persistence_adapter(self) -> InMemoryPersistenceAdapter:
        return InMemoryPersistenceAdapter()

    @bean
    def logger_events_adapter(self) -> LoggerEventsAdapter:
        return LoggerEventsAdapter()

    # -- Saga engine components ---------------------------------------------

    @bean
    def saga_argument_resolver(self) -> ArgumentResolver:
        return ArgumentResolver()

    @bean
    def saga_step_invoker(self, argument_resolver: ArgumentResolver) -> StepInvoker:
        return StepInvoker(argument_resolver=argument_resolver)

    @bean
    def saga_compensator(
        self,
        step_invoker: StepInvoker,
        events_adapter: LoggerEventsAdapter,
    ) -> SagaCompensator:
        return SagaCompensator(step_invoker=step_invoker, events_port=events_adapter)

    @bean
    def saga_execution_orchestrator(
        self,
        step_invoker: StepInvoker,
        events_adapter: LoggerEventsAdapter,
    ) -> SagaExecutionOrchestrator:
        return SagaExecutionOrchestrator(step_invoker=step_invoker, events_port=events_adapter)

    @bean
    def saga_registry(self) -> SagaRegistry:
        return SagaRegistry()

    @bean
    def saga_engine(
        self,
        registry: SagaRegistry,
        step_invoker: StepInvoker,
        execution_orchestrator: SagaExecutionOrchestrator,
        compensator: SagaCompensator,
        persistence_adapter: InMemoryPersistenceAdapter,
        events_adapter: LoggerEventsAdapter,
    ) -> SagaEngine:
        return SagaEngine(
            registry=registry,
            step_invoker=step_invoker,
            execution_orchestrator=execution_orchestrator,
            compensator=compensator,
            persistence_port=persistence_adapter,
            events_port=events_adapter,
        )

    # -- TCC engine components ----------------------------------------------

    @bean
    def tcc_registry(self) -> TccRegistry:
        return TccRegistry()

    @bean
    def tcc_engine(
        self,
        tcc_registry: TccRegistry,
        persistence_adapter: InMemoryPersistenceAdapter,
        events_adapter: LoggerEventsAdapter,
    ) -> TccEngine:
        tcc_argument_resolver = TccArgumentResolver()
        tcc_invoker = TccParticipantInvoker(argument_resolver=tcc_argument_resolver)
        tcc_orchestrator = TccExecutionOrchestrator(participant_invoker=tcc_invoker)
        return TccEngine(
            registry=tcc_registry,
            participant_invoker=tcc_invoker,
            orchestrator=tcc_orchestrator,
            persistence_port=persistence_adapter,
            events_port=events_adapter,
        )

    # -- Workflow engine components -----------------------------------------

    @bean
    def signal_service(self) -> SignalService:
        return SignalService()

    @bean
    def timer_service(self) -> TimerService:
        return TimerService()

    @bean
    def child_workflow_service(self) -> ChildWorkflowService:
        return ChildWorkflowService()

    @bean
    def continue_as_new_service(self) -> ContinueAsNewService:
        return ContinueAsNewService()

    @bean
    def workflow_query_service(self) -> WorkflowQueryService:
        return WorkflowQueryService()

    @bean
    def workflow_registry(self) -> WorkflowRegistry:
        return WorkflowRegistry()

    @bean
    def orchestration_bean_post_processor(
        self,
        saga_registry: SagaRegistry,
        workflow_registry: WorkflowRegistry,
        tcc_registry: TccRegistry,
        scheduler: OrchestrationScheduler,
        workflow_engine: WorkflowEngine,
        tcc_engine: TccEngine,
        saga_engine: SagaEngine,
    ) -> OrchestrationBeanPostProcessor:
        # Auto-discovers @saga/@workflow/@tcc beans into their registries and
        # schedules any @scheduled_* orchestration during startup (audit #53/#54).
        return OrchestrationBeanPostProcessor(
            saga_registry=saga_registry,
            workflow_registry=workflow_registry,
            tcc_registry=tcc_registry,
            scheduler=scheduler,
            workflow_engine=workflow_engine,
            tcc_engine=tcc_engine,
            saga_engine=saga_engine,
        )

    @bean
    def workflow_executor(
        self,
        signal_service: SignalService,
        timer_service: TimerService,
        child_service: ChildWorkflowService,
        events: CompositeOrchestrationEvents,
        invoker: CoreStepInvoker,
    ) -> WorkflowExecutor:
        return WorkflowExecutor(
            step_invoker=invoker,
            signal_service=signal_service,
            timer_service=timer_service,
            child_service=child_service,
            events=events,
        )

    @bean
    def workflow_engine(
        self,
        registry: WorkflowRegistry,
        executor: WorkflowExecutor,
        persistence: ExecutionPersistenceProvider,
        events: CompositeOrchestrationEvents,
        signals: SignalService,
        queries: WorkflowQueryService,
        children: ChildWorkflowService,
        cont: ContinueAsNewService,
        dlq: DeadLetterService,
    ) -> WorkflowEngine:
        return WorkflowEngine(
            registry=registry,
            executor=executor,
            persistence=persistence,
            events=events,
            signal_service=signals,
            query_service=queries,
            child_service=children,
            continue_service=cont,
            dead_letter_service=dlq,
        )

    # -- Recovery and REST --------------------------------------------------

    @bean
    def saga_recovery_service(
        self,
        persistence_adapter: InMemoryPersistenceAdapter,
        saga_engine: SagaEngine,
        events_adapter: LoggerEventsAdapter,
    ) -> SagaRecoveryService:
        return SagaRecoveryService(
            persistence_port=persistence_adapter,
            saga_engine=saga_engine,
            events_port=events_adapter,
        )

    @bean
    def orchestration_health_indicator(self, persistence: ExecutionPersistenceProvider) -> OrchestrationHealthIndicator:
        return OrchestrationHealthIndicator(persistence=persistence)

    @bean
    def orchestration_controller(self, persistence: ExecutionPersistenceProvider) -> OrchestrationController:
        return OrchestrationController(persistence=persistence)

    @bean
    def dead_letter_controller(self, dlq: DeadLetterService) -> DeadLetterController:
        return DeadLetterController(dlq=dlq)

    @bean
    def workflow_controller(self, engine: WorkflowEngine) -> WorkflowController:
        return WorkflowController(engine=engine)
