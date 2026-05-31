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
"""Regressions for AOP weaving: @property getters must not be evaluated during
weaving, and advice must be woven regardless of target/aspect registration order."""

from __future__ import annotations

import pytest

from pyfly.aop.decorators import aspect, before
from pyfly.aop.post_processor import AspectBeanPostProcessor
from pyfly.aop.types import JoinPoint
from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


class TestWeavingDoesNotTriggerProperties:
    @pytest.mark.asyncio
    async def test_side_effecting_property_not_evaluated_during_weaving(self) -> None:
        calls: list[str] = []

        @service
        class ServiceWithProperty:
            def __init__(self) -> None:
                self.property_reads = 0

            @property
            def dangerous(self) -> str:
                # A property that raises would previously abort startup because
                # weaving did getattr() over every public attribute.
                self.property_reads += 1
                raise RuntimeError("property must not be evaluated during weaving")

            async def do_work(self) -> str:
                return "done"

        @aspect
        class WorkAspect:
            @before("service.ServiceWithProperty.*")
            def on_before(self, jp: JoinPoint) -> None:
                calls.append("before")

        ctx = ApplicationContext(Config())
        ctx.register_bean(WorkAspect)
        ctx.register_bean(ServiceWithProperty)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()  # must not raise

        svc = ctx.get_bean(ServiceWithProperty)
        assert svc.property_reads == 0  # weaving never touched the property
        assert await svc.do_work() == "done"
        assert calls == ["before"]  # the real method was still woven


class TestWeavingIsOrderIndependent:
    @pytest.mark.asyncio
    async def test_target_registered_before_aspect_is_still_woven(self) -> None:
        calls: list[str] = []

        @service
        class OrderTarget:
            async def execute(self) -> str:
                return "ran"

        @aspect
        class OrderAspect:
            @before("service.OrderTarget.*")
            def on_before(self, jp: JoinPoint) -> None:
                calls.append("before")

        ctx = ApplicationContext(Config())
        # Register the TARGET *before* the ASPECT — previously this left the
        # target unwoven because its after_init ran before the aspect was
        # collected.
        ctx.register_bean(OrderTarget)
        ctx.register_bean(OrderAspect)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        target = ctx.get_bean(OrderTarget)
        assert await target.execute() == "ran"
        assert calls == ["before"]
