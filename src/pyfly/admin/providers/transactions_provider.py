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
"""Transactions data provider — saga and TCC definition listing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class TransactionsProvider:
    """Provides saga and TCC transaction information for the admin dashboard."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_transactions(self) -> dict[str, Any]:
        sagas = await self._collect_sagas()
        tccs = await self._collect_tccs()
        in_flight = await self._get_in_flight_count()

        return {
            "sagas": sagas,
            "tcc": tccs,
            "saga_count": len(sagas),
            "tcc_count": len(tccs),
            "total": len(sagas) + len(tccs),
            "in_flight": in_flight,
        }

    async def _collect_sagas(self) -> list[dict[str, Any]]:
        sagas: list[dict[str, Any]] = []
        try:
            from pyfly.transactional.saga.registry.saga_registry import SagaRegistry

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, SagaRegistry):
                    for saga_def in reg.instance.get_all():
                        steps = []
                        for step_id, step_def in saga_def.steps.items():
                            steps.append(
                                {
                                    "id": step_id,
                                    "depends_on": list(step_def.depends_on),
                                    "has_compensation": step_def.compensate_method is not None,
                                    "retry": step_def.retry,
                                    "backoff_ms": step_def.backoff_ms,
                                    "timeout_ms": step_def.timeout_ms,
                                    "cpu_bound": step_def.cpu_bound,
                                    "idempotency_key": step_def.idempotency_key,
                                }
                            )
                        sagas.append(
                            {
                                "name": saga_def.name,
                                "type": f"{type(saga_def.bean).__module__}.{type(saga_def.bean).__qualname__}",
                                "layer_concurrency": saga_def.layer_concurrency,
                                "step_count": len(saga_def.steps),
                                "steps": steps,
                            }
                        )
        except ImportError:
            pass
        return sagas

    async def _collect_tccs(self) -> list[dict[str, Any]]:
        tccs: list[dict[str, Any]] = []
        try:
            from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, TccRegistry):
                    for tcc_def in reg.instance.get_all():
                        participants = []
                        for pid, pdef in tcc_def.participants.items():
                            participants.append(
                                {
                                    "id": pid,
                                    "order": pdef.order,
                                    "optional": pdef.optional,
                                    "timeout_ms": pdef.timeout_ms,
                                    "has_try": pdef.try_method is not None,
                                    "has_confirm": pdef.confirm_method is not None,
                                    "has_cancel": pdef.cancel_method is not None,
                                }
                            )
                        tccs.append(
                            {
                                "name": tcc_def.name,
                                "type": f"{type(tcc_def.bean).__module__}.{type(tcc_def.bean).__qualname__}",
                                "timeout_ms": tcc_def.timeout_ms,
                                "retry_enabled": tcc_def.retry_enabled,
                                "max_retries": tcc_def.max_retries,
                                "backoff_ms": tcc_def.backoff_ms,
                                "participant_count": len(tcc_def.participants),
                                "participants": participants,
                            }
                        )
        except ImportError:
            pass
        return tccs

    async def _get_in_flight_count(self) -> int:
        try:
            from pyfly.transactional.shared.persistence.memory import (
                InMemoryPersistenceAdapter,
            )

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, InMemoryPersistenceAdapter):
                    in_flight = await reg.instance.get_in_flight()
                    return len(in_flight)
        except ImportError:
            pass
        return 0
