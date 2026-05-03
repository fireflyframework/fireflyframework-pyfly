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
"""End-to-end AOP integration tests — full ApplicationContext lifecycle.

Each test registers @service and @aspect beans with the ApplicationContext,
starts the context (triggering AspectBeanPostProcessor weaving), then
exercises the woven methods to verify correct advice execution order.
"""

from __future__ import annotations

import pytest

from pyfly.aop.decorators import (
    after,
    after_returning,
    after_throwing,
    around,
    aspect,
    before,
)
from pyfly.aop.post_processor import AspectBeanPostProcessor
from pyfly.aop.types import JoinPoint
from pyfly.container.ordering import order
from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config

# ---------------------------------------------------------------------------
# Test 1 — Full advice chain (all 5 advice types in correct order)
# ---------------------------------------------------------------------------


class TestFullAdviceChain:
    """@before, @after_returning, and @after fire in sequence on success."""

    @pytest.mark.asyncio
    async def test_all_advice_types_fire_in_order(self) -> None:
        calls: list[str] = []

        @service
        class Calculator:
            async def add(self, a: int, b: int) -> int:
                calls.append("method")
                return a + b

        @aspect
        class CalculatorAspect:
            @before("service.Calculator.*")
            def on_before(self, jp: JoinPoint) -> None:
                calls.append("before")

            @after_returning("service.Calculator.*")
            def on_after_returning(self, jp: JoinPoint) -> None:
                calls.append("after_returning")

            @after("service.Calculator.*")
            def on_after(self, jp: JoinPoint) -> None:
                calls.append("after")

        ctx = ApplicationContext(Config())
        ctx.register_bean(CalculatorAspect)
        ctx.register_bean(Calculator)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        calc = ctx.get_bean(Calculator)
        result = await calc.add(2, 3)

        assert result == 5
        assert calls == ["before", "method", "after_returning", "after"]


# ---------------------------------------------------------------------------
# Test 2 — @around modifies the return value
# ---------------------------------------------------------------------------


class TestAroundWithModification:
    """@around advice wraps proceed() and can transform the result."""

    @pytest.mark.asyncio
    async def test_around_prepends_cached(self) -> None:
        @service
        class SlowService:
            async def fetch(self, key: str) -> str:
                return f"value_for_{key}"

        @aspect
        class CacheAspect:
            @around("service.SlowService.*")
            async def cache_wrap(self, jp: JoinPoint) -> str:
                result = await jp.proceed(*jp.args, **jp.kwargs)
                return f"cached:{result}"

        ctx = ApplicationContext(Config())
        ctx.register_bean(CacheAspect)
        ctx.register_bean(SlowService)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        svc = ctx.get_bean(SlowService)
        result = await svc.fetch("test")

        assert result == "cached:value_for_test"


# ---------------------------------------------------------------------------
# Test 3 — Exception handling: @after_throwing + @after both fire
# ---------------------------------------------------------------------------


class TestExceptionHandlingFlow:
    """@after_throwing and @after fire when the method raises, and the
    original exception propagates unchanged."""

    @pytest.mark.asyncio
    async def test_after_throwing_and_after_fire_on_error(self) -> None:
        calls: list[str] = []

        @service
        class RiskyService:
            async def risky_op(self) -> str:
                raise ValueError("something went wrong")

        @aspect
        class ErrorAspect:
            @after_throwing("service.RiskyService.*")
            def on_throw(self, jp: JoinPoint) -> None:
                calls.append(f"after_throwing:{jp.exception}")

            @after("service.RiskyService.*")
            def on_after(self, jp: JoinPoint) -> None:
                calls.append("after")

        ctx = ApplicationContext(Config())
        ctx.register_bean(ErrorAspect)
        ctx.register_bean(RiskyService)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        svc = ctx.get_bean(RiskyService)

        with pytest.raises(ValueError, match="something went wrong"):
            await svc.risky_op()

        assert "after_throwing:something went wrong" in calls
        assert "after" in calls


# ---------------------------------------------------------------------------
# Test 4 — Multiple aspects with @order
# ---------------------------------------------------------------------------


class TestMultipleAspectsOrdered:
    """Three @aspect beans at different @order values execute @before
    advice in ascending order (lowest first)."""

    @pytest.mark.asyncio
    async def test_aspects_execute_in_order(self) -> None:
        calls: list[str] = []

        @service
        class PaymentService:
            async def charge(self, amount: int) -> str:
                return f"charged:{amount}"

        @aspect
        @order(1)
        class LoggingAspect:
            @before("service.PaymentService.*")
            def log(self, jp: JoinPoint) -> None:
                calls.append("logging")

        @aspect
        @order(5)
        class MetricsAspect:
            @before("service.PaymentService.*")
            def metric(self, jp: JoinPoint) -> None:
                calls.append("metrics")

        @aspect
        @order(10)
        class SecurityAspect:
            @before("service.PaymentService.*")
            def secure(self, jp: JoinPoint) -> None:
                calls.append("security")

        ctx = ApplicationContext(Config())
        # Register in deliberate non-order to prove sorting works
        ctx.register_bean(SecurityAspect)
        ctx.register_bean(LoggingAspect)
        ctx.register_bean(MetricsAspect)
        ctx.register_bean(PaymentService)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        svc = ctx.get_bean(PaymentService)
        result = await svc.charge(100)

        assert result == "charged:100"
        assert calls == ["logging", "metrics", "security"]


# ---------------------------------------------------------------------------
# Test 5 — Sync method weaving with @before
# ---------------------------------------------------------------------------


class TestSyncMethodWeaving:
    """Sync (non-async) bean methods are woven correctly by the
    AspectBeanPostProcessor and @before advice runs."""

    @pytest.mark.asyncio
    async def test_sync_method_with_before_advice(self) -> None:
        calls: list[str] = []

        @service
        class Formatter:
            def format_name(self, first: str, last: str) -> str:
                return f"{first} {last}"

        @aspect
        class FormatterAspect:
            @before("service.Formatter.*")
            def on_before(self, jp: JoinPoint) -> None:
                calls.append(f"before:{jp.method_name}")

        ctx = ApplicationContext(Config())
        ctx.register_bean(FormatterAspect)
        ctx.register_bean(Formatter)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        fmt = ctx.get_bean(Formatter)
        result = fmt.format_name("Jane", "Doe")

        assert result == "Jane Doe"
        assert calls == ["before:format_name"]


# ---------------------------------------------------------------------------
# Test 6 — Multiple @around advice chained correctly
# ---------------------------------------------------------------------------


class TestMultipleAroundChaining:
    """Two @around aspects on the same method both execute, with the
    outermost (lowest @order) wrapping the inner."""

    @pytest.mark.asyncio
    async def test_multiple_around_chain_in_order(self) -> None:
        calls: list[str] = []

        @service
        class DataService:
            async def get_data(self, key: str) -> str:
                calls.append("original")
                return f"data:{key}"

        @aspect
        @order(1)
        class OuterAspect:
            @around("service.DataService.*")
            async def outer_wrap(self, jp: JoinPoint) -> str:
                calls.append("outer:before")
                result = await jp.proceed()
                calls.append("outer:after")
                return f"outer({result})"

        @aspect
        @order(10)
        class InnerAspect:
            @around("service.DataService.*")
            async def inner_wrap(self, jp: JoinPoint) -> str:
                calls.append("inner:before")
                result = await jp.proceed()
                calls.append("inner:after")
                return f"inner({result})"

        ctx = ApplicationContext(Config())
        ctx.register_bean(InnerAspect)
        ctx.register_bean(OuterAspect)
        ctx.register_bean(DataService)
        ctx.register_post_processor(AspectBeanPostProcessor())
        await ctx.start()

        svc = ctx.get_bean(DataService)
        result = await svc.get_data("x")

        # Outer wraps inner wraps original
        assert calls == [
            "outer:before",
            "inner:before",
            "original",
            "inner:after",
            "outer:after",
        ]
        assert result == "outer(inner(data:x))"
