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
"""Auto-discovery of @saga / @workflow / @tcc beans into their registries.

Mirrors the Java ``WorkflowRegistry.ensureScanned`` / ``SchedulingPostProcessor``
behaviour: every bean carrying orchestration metadata is registered with the
matching registry as the ApplicationContext initialises it, and any
``@scheduled_*`` orchestration is turned into a live :class:`ScheduledTask` so a
declared ``@workflow`` / ``@tcc`` / ``@saga`` bean is actually runnable and its
schedule actually fires — without the user wiring anything (audit #53, #54).
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.transactional.core.scheduling import OrchestrationScheduler, ScheduledTask
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.registry import WorkflowRegistry

logger = logging.getLogger(__name__)


class OrchestrationBeanPostProcessor:
    """Registers orchestration beans into registries and schedules them."""

    def __init__(
        self,
        saga_registry: SagaRegistry,
        workflow_registry: WorkflowRegistry,
        tcc_registry: TccRegistry,
        scheduler: OrchestrationScheduler,
        workflow_engine: WorkflowEngine,
        tcc_engine: TccEngine,
        saga_engine: SagaEngine,
    ) -> None:
        self._saga_registry = saga_registry
        self._workflow_registry = workflow_registry
        self._tcc_registry = tcc_registry
        self._scheduler = scheduler
        self._workflow_engine = workflow_engine
        self._tcc_engine = tcc_engine
        self._saga_engine = saga_engine

    def before_init(self, bean: Any, bean_name: str) -> Any:
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        cls = type(bean)

        if getattr(cls, "__pyfly_saga__", None) is not None:
            self._register(self._saga_registry, bean, "saga")
            saga_name = cls.__pyfly_saga__.get("name", cls.__name__)
            self._schedule(
                cls,
                "__pyfly_saga_scheduled__",
                f"saga:{saga_name}",
                lambda sched, name=saga_name: self._saga_engine.execute(name, input_data=getattr(sched, "input", None)),
            )

        if getattr(cls, "__pyfly_workflow__", None) is not None:
            self._register(self._workflow_registry, bean, "workflow")
            wf_id = cls.__pyfly_workflow__.id
            self._schedule(
                cls,
                "__pyfly_workflow_scheduled__",
                f"workflow:{wf_id}",
                lambda sched, wid=wf_id: self._workflow_engine.start(wid),
            )

        if getattr(cls, "__pyfly_tcc__", None) is not None:
            self._register(self._tcc_registry, bean, "tcc")
            tcc_name = cls.__pyfly_tcc__.get("name", cls.__name__)
            self._schedule(
                cls,
                "__pyfly_tcc_scheduled__",
                f"tcc:{tcc_name}",
                lambda sched, name=tcc_name: self._tcc_engine.execute(name, input_data=getattr(sched, "input", None)),
            )

        return bean

    def _schedule(self, cls: type, meta_attr: str, task_id: str, runner: Any) -> None:
        schedules = getattr(cls, meta_attr, None)
        if not schedules:
            return

        for index, sched in enumerate(schedules):
            full_id = task_id if index == 0 else f"{task_id}#{index}"

            async def _callback(_sched: Any = sched, _runner: Any = runner) -> None:
                await _runner(_sched)

            task = ScheduledTask(
                id=full_id,
                callback=_callback,
                cron=sched.cron or None,
                fixed_rate_ms=sched.fixed_rate_ms,
                fixed_delay_ms=sched.fixed_delay_ms,
                initial_delay_ms=sched.initial_delay_ms,
                enabled=sched.enabled,
            )
            if not task.has_valid_trigger():
                logger.warning("scheduled_orchestration_no_trigger", extra={"task": full_id})
                continue
            self._scheduler.register(task)

    @staticmethod
    def _register(registry: Any, bean: Any, kind: str) -> None:
        try:
            registry.register_from_bean(bean)
        except Exception:  # pragma: no cover - defensive; validation errors logged
            logger.warning(
                "orchestration_registration_failed",
                extra={"kind": kind, "bean": type(bean).__qualname__},
                exc_info=True,
            )
