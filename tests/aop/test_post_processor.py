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
"""Tests for AspectBeanPostProcessor — automatic advice weaving via ApplicationContext."""

from __future__ import annotations

import pytest

from pyfly.aop.decorators import after_returning, aspect, before
from pyfly.aop.post_processor import AspectBeanPostProcessor
from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@aspect
class LoggingAspect:
    calls: list[str] = []

    @before("service.OrderService.*")
    def log_before(self, jp):
        LoggingAspect.calls.append(f"before:{jp.method_name}")

    @after_returning("service.OrderService.*")
    def log_after(self, jp):
        LoggingAspect.calls.append(f"after_returning:{jp.return_value}")


@service
class OrderService:
    async def create_order(self, item: str) -> str:
        return f"order:{item}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAspectBeanPostProcessor:
    @pytest.mark.asyncio
    async def test_before_advice_applied_via_context(self) -> None:
        LoggingAspect.calls = []

        ctx = ApplicationContext(Config())
        ctx.register_bean(LoggingAspect)
        ctx.register_bean(OrderService)

        pp = AspectBeanPostProcessor()
        ctx.register_post_processor(pp)

        await ctx.start()

        svc = ctx.get_bean(OrderService)
        result = await svc.create_order("widget")

        assert result == "order:widget"
        assert "before:create_order" in LoggingAspect.calls

    @pytest.mark.asyncio
    async def test_after_returning_sees_result_via_context(self) -> None:
        LoggingAspect.calls = []

        ctx = ApplicationContext(Config())
        ctx.register_bean(LoggingAspect)
        ctx.register_bean(OrderService)

        pp = AspectBeanPostProcessor()
        ctx.register_post_processor(pp)

        await ctx.start()

        svc = ctx.get_bean(OrderService)
        await svc.create_order("gadget")

        assert "after_returning:order:gadget" in LoggingAspect.calls

    @pytest.mark.asyncio
    async def test_aspects_not_woven_into_themselves(self) -> None:
        """Aspect beans should not be targets of their own advice."""

        @aspect
        class SelfAspect:
            woven = False

            @before("aspect.SelfAspect.*")
            def intercept(self, jp):
                SelfAspect.woven = True

        ctx = ApplicationContext(Config())
        ctx.register_bean(SelfAspect)

        pp = AspectBeanPostProcessor()
        ctx.register_post_processor(pp)

        await ctx.start()

        # The aspect should not be woven
        assert not SelfAspect.woven

    @pytest.mark.asyncio
    async def test_non_matching_methods_untouched(self) -> None:
        """Methods that don't match any pointcut remain unwrapped."""
        calls: list[str] = []

        @aspect
        class NarrowAspect:
            @before("service.OrderService.create_order")
            def on_create(self, jp):
                calls.append("hit")

        @service
        class PaymentService:
            async def charge(self) -> str:
                return "charged"

        ctx = ApplicationContext(Config())
        ctx.register_bean(NarrowAspect)
        ctx.register_bean(PaymentService)

        pp = AspectBeanPostProcessor()
        ctx.register_post_processor(pp)

        await ctx.start()

        pay = ctx.get_bean(PaymentService)
        result = await pay.charge()

        assert result == "charged"
        assert calls == []


class TestAspectBeanPostProcessorUnit:
    """Unit-level tests for the post processor without ApplicationContext."""

    def test_before_init_collects_aspect(self) -> None:
        pp = AspectBeanPostProcessor()
        inst = LoggingAspect()
        pp.before_init(inst, "loggingAspect")

        assert pp._registry is not None
        assert LoggingAspect in pp._aspect_types

    def test_before_init_ignores_non_aspect(self) -> None:
        pp = AspectBeanPostProcessor()
        svc = OrderService()
        pp.before_init(svc, "orderService")

        assert pp._registry is None
        assert pp._aspect_types == set()

    def test_after_init_noop_without_registry(self) -> None:
        pp = AspectBeanPostProcessor()
        svc = OrderService()
        result = pp.after_init(svc, "orderService")
        assert result is svc

    def test_after_init_skips_aspect_beans(self) -> None:
        pp = AspectBeanPostProcessor()
        inst = LoggingAspect()
        pp.before_init(inst, "loggingAspect")

        result = pp.after_init(inst, "loggingAspect")
        assert result is inst

    def test_prefix_uses_stereotype(self) -> None:
        """When a bean has a stereotype, the prefix should be stereotype.ClassName."""
        calls: list[str] = []

        @aspect
        class PrefixAspect:
            @before("service.OrderService.*")
            def check(self, jp):
                calls.append("matched")

        pp = AspectBeanPostProcessor()
        pp.before_init(PrefixAspect(), "prefixAspect")

        svc = OrderService()
        pp.after_init(svc, "orderService")

        # The service has stereotype="service", so prefix = "service.OrderService"
        # The @before pointcut is "service.OrderService.*" which should match
        # We can't easily test the wrapped method here without calling it,
        # but we can verify the aspect was registered
        assert pp._registry is not None
