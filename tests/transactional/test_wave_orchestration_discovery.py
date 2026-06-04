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
"""@workflow / @tcc / @saga beans are auto-discovered into the registries (audit #53)."""

from __future__ import annotations

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.tcc.annotations import (
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry
from pyfly.transactional.workflow.annotations import workflow, workflow_step
from pyfly.transactional.workflow.registry import WorkflowRegistry


@workflow(id="discoveredWorkflow")
class DiscoveredWorkflow:
    @workflow_step(id="step1")
    async def step1(self) -> str:
        return "ok"


@tcc(name="discovered-tcc")
class DiscoveredTcc:
    @tcc_participant(id="p1")
    class P1:
        @try_method()
        async def do_try(self) -> str:
            return "t"

        @confirm_method()
        async def do_confirm(self) -> None: ...

        @cancel_method()
        async def do_cancel(self) -> None: ...


@saga(name="discovered-saga")
class DiscoveredSaga:
    @saga_step(id="s1")
    async def s1(self) -> str:
        return "ok"


def _ctx() -> ApplicationContext:
    return ApplicationContext(Config({"pyfly": {"transactional": {"enabled": "true"}}}))


@pytest.mark.asyncio
async def test_workflow_bean_auto_registered() -> None:
    ctx = _ctx()
    ctx.register_bean(DiscoveredWorkflow)
    await ctx.start()

    registry = ctx.get_bean(WorkflowRegistry)
    assert registry.get("discoveredWorkflow") is not None


@pytest.mark.asyncio
async def test_tcc_bean_auto_registered() -> None:
    ctx = _ctx()
    ctx.register_bean(DiscoveredTcc)
    await ctx.start()

    registry = ctx.get_bean(TccRegistry)
    assert registry.get("discovered-tcc") is not None


@pytest.mark.asyncio
async def test_saga_bean_auto_registered() -> None:
    ctx = _ctx()
    ctx.register_bean(DiscoveredSaga)
    await ctx.start()

    registry = ctx.get_bean(SagaRegistry)
    assert registry.get("discovered-saga") is not None
