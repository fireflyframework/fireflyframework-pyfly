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
"""@scheduled_workflow / @scheduled_tcc / @scheduled_saga actually fire (audit #54)."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.transactional.core.scheduling import OrchestrationScheduler
from pyfly.transactional.workflow.annotations import scheduled_workflow, workflow, workflow_step

_FIRED: list[str] = []


@scheduled_workflow(fixed_rate_ms=30)
@workflow(id="scheduledWf")
class ScheduledWf:
    @workflow_step(id="tick")
    async def tick(self) -> str:
        _FIRED.append("tick")
        return "ok"


@pytest.mark.asyncio
async def test_scheduled_workflow_fires() -> None:
    _FIRED.clear()
    ctx = ApplicationContext(Config({"pyfly": {"transactional": {"enabled": "true"}}}))
    ctx.register_bean(ScheduledWf)
    await ctx.start()
    try:
        # The scheduler is started by the context lifecycle and the task was
        # registered during post-processing; give it time to fire a few times.
        await asyncio.sleep(0.12)
        assert len(_FIRED) >= 1, "scheduled workflow never fired"

        scheduler = ctx.get_bean(OrchestrationScheduler)
        assert any(t.id == "workflow:scheduledWf" for t in scheduler.list())
    finally:
        await ctx.stop()
