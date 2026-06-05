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
"""Tests for AOP weaver — method wrapping with advice chain."""

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
from pyfly.aop.registry import AspectRegistry
from pyfly.aop.weaver import weave_bean

# ---------------------------------------------------------------------------
# Helper beans and aspects
# ---------------------------------------------------------------------------


class MyService:
    """A simple service with async and sync methods."""

    async def greet(self, name: str) -> str:
        return f"hello {name}"

    async def explode(self) -> str:
        raise ValueError("boom")

    def sync_greet(self, name: str) -> str:
        return f"hi {name}"

    def sync_explode(self) -> str:
        raise RuntimeError("sync boom")


def _make_registry(*aspects_instances: object) -> AspectRegistry:
    registry = AspectRegistry()
    for inst in aspects_instances:
        registry.register(inst)
    return registry


# ---------------------------------------------------------------------------
# @before
# ---------------------------------------------------------------------------


class TestBeforeAdvice:
    @pytest.mark.asyncio
    async def test_before_runs_before_async_method(self) -> None:
        calls: list[str] = []

        @aspect
        class LogAspect:
            @before("service.MyService.*")
            def log_before(self, jp):
                calls.append(f"before:{jp.method_name}")

        svc = MyService()
        registry = _make_registry(LogAspect())
        weave_bean(svc, "service.MyService", registry)

        result = await svc.greet("alice")
        assert result == "hello alice"
        assert calls == ["before:greet"]

    def test_before_runs_on_sync_method(self) -> None:
        calls: list[str] = []

        @aspect
        class SyncLogAspect:
            @before("service.MyService.*")
            def log_before(self, jp):
                calls.append(f"before:{jp.method_name}")

        svc = MyService()
        registry = _make_registry(SyncLogAspect())
        weave_bean(svc, "service.MyService", registry)

        result = svc.sync_greet("bob")
        assert result == "hi bob"
        assert calls == ["before:sync_greet"]


# ---------------------------------------------------------------------------
# @after_returning
# ---------------------------------------------------------------------------


class TestAfterReturningAdvice:
    @pytest.mark.asyncio
    async def test_after_returning_sees_return_value(self) -> None:
        captured: list = []

        @aspect
        class ReturnAspect:
            @after_returning("service.MyService.*")
            def on_return(self, jp):
                captured.append(jp.return_value)

        svc = MyService()
        registry = _make_registry(ReturnAspect())
        weave_bean(svc, "service.MyService", registry)

        result = await svc.greet("world")
        assert result == "hello world"
        assert captured == ["hello world"]

    def test_after_returning_sync(self) -> None:
        captured: list = []

        @aspect
        class SyncReturnAspect:
            @after_returning("service.MyService.*")
            def on_return(self, jp):
                captured.append(jp.return_value)

        svc = MyService()
        registry = _make_registry(SyncReturnAspect())
        weave_bean(svc, "service.MyService", registry)

        result = svc.sync_greet("eve")
        assert result == "hi eve"
        assert captured == ["hi eve"]


# ---------------------------------------------------------------------------
# @after_throwing
# ---------------------------------------------------------------------------


class TestAfterThrowingAdvice:
    @pytest.mark.asyncio
    async def test_after_throwing_sees_exception(self) -> None:
        captured: list = []

        @aspect
        class ErrorAspect:
            @after_throwing("service.MyService.*")
            def on_error(self, jp):
                captured.append(str(jp.exception))

        svc = MyService()
        registry = _make_registry(ErrorAspect())
        weave_bean(svc, "service.MyService", registry)

        with pytest.raises(ValueError, match="boom"):
            await svc.explode()

        assert captured == ["boom"]

    def test_after_throwing_sync(self) -> None:
        captured: list = []

        @aspect
        class SyncErrorAspect:
            @after_throwing("service.MyService.*")
            def on_error(self, jp):
                captured.append(str(jp.exception))

        svc = MyService()
        registry = _make_registry(SyncErrorAspect())
        weave_bean(svc, "service.MyService", registry)

        with pytest.raises(RuntimeError, match="sync boom"):
            svc.sync_explode()

        assert captured == ["sync boom"]

    @pytest.mark.asyncio
    async def test_after_throwing_not_called_on_success(self) -> None:
        captured: list = []

        @aspect
        class ErrorAspect:
            @after_throwing("service.MyService.*")
            def on_error(self, jp):
                captured.append("should not happen")

        svc = MyService()
        registry = _make_registry(ErrorAspect())
        weave_bean(svc, "service.MyService", registry)

        await svc.greet("ok")
        assert captured == []


# ---------------------------------------------------------------------------
# @after (always runs)
# ---------------------------------------------------------------------------


class TestAfterAdvice:
    @pytest.mark.asyncio
    async def test_after_runs_on_success(self) -> None:
        calls: list[str] = []

        @aspect
        class AfterAspect:
            @after("service.MyService.*")
            def on_after(self, jp):
                calls.append(f"after:{jp.method_name}")

        svc = MyService()
        registry = _make_registry(AfterAspect())
        weave_bean(svc, "service.MyService", registry)

        await svc.greet("ok")
        assert "after:greet" in calls

    @pytest.mark.asyncio
    async def test_after_runs_on_exception(self) -> None:
        calls: list[str] = []

        @aspect
        class AfterAspect:
            @after("service.MyService.*")
            def on_after(self, jp):
                calls.append(f"after:{jp.method_name}")

        svc = MyService()
        registry = _make_registry(AfterAspect())
        weave_bean(svc, "service.MyService", registry)

        with pytest.raises(ValueError):
            await svc.explode()

        assert "after:explode" in calls


# ---------------------------------------------------------------------------
# @around
# ---------------------------------------------------------------------------


class TestAroundAdvice:
    @pytest.mark.asyncio
    async def test_around_wraps_execution(self) -> None:
        calls: list[str] = []

        @aspect
        class AroundAspect:
            @around("service.MyService.*")
            async def wrap(self, jp):
                calls.append("around:before")
                result = await jp.proceed(*jp.args, **jp.kwargs)
                calls.append("around:after")
                return result

        svc = MyService()
        registry = _make_registry(AroundAspect())
        weave_bean(svc, "service.MyService", registry)

        result = await svc.greet("x")
        assert result == "hello x"
        assert calls == ["around:before", "around:after"]

    @pytest.mark.asyncio
    async def test_around_can_modify_result(self) -> None:
        @aspect
        class ModifyAspect:
            @around("service.MyService.*")
            async def wrap(self, jp):
                result = await jp.proceed(*jp.args, **jp.kwargs)
                return result.upper()

        svc = MyService()
        registry = _make_registry(ModifyAspect())
        weave_bean(svc, "service.MyService", registry)

        result = await svc.greet("x")
        assert result == "HELLO X"

    def test_around_wraps_sync_method(self) -> None:
        """@around must apply to synchronous joinpoints too (audit #114)."""
        calls: list[str] = []

        @aspect
        class SyncAroundAspect:
            @around("service.MyService.*")
            def wrap(self, jp):
                calls.append("around:before")
                result = jp.proceed(*jp.args, **jp.kwargs)
                calls.append("around:after")
                return result.upper()

        svc = MyService()
        registry = _make_registry(SyncAroundAspect())
        weave_bean(svc, "service.MyService", registry)

        result = svc.sync_greet("x")
        assert result == "HI X"  # @around modified the return value
        assert calls == ["around:before", "around:after"]


# ---------------------------------------------------------------------------
# Multiple aspects — ordering
# ---------------------------------------------------------------------------


class TestMultipleAspectsOrdering:
    @pytest.mark.asyncio
    async def test_before_advice_ordered_by_aspect_order(self) -> None:
        """Lower @order aspects run first."""
        from pyfly.container.ordering import order

        calls: list[str] = []

        @aspect
        @order(10)
        class SecondAspect:
            @before("service.MyService.*")
            def second(self, jp):
                calls.append("second")

        @aspect
        @order(1)
        class FirstAspect:
            @before("service.MyService.*")
            def first(self, jp):
                calls.append("first")

        svc = MyService()
        # Register in wrong order to prove sorting works
        registry = _make_registry(SecondAspect(), FirstAspect())
        weave_bean(svc, "service.MyService", registry)

        await svc.greet("x")
        assert calls == ["first", "second"]

    @pytest.mark.asyncio
    async def test_combined_before_and_after_returning(self) -> None:
        calls: list[str] = []

        @aspect
        class CombinedAspect:
            @before("service.MyService.*")
            def on_before(self, jp):
                calls.append("before")

            @after_returning("service.MyService.*")
            def on_return(self, jp):
                calls.append(f"after_returning:{jp.return_value}")

        svc = MyService()
        registry = _make_registry(CombinedAspect())
        weave_bean(svc, "service.MyService", registry)

        result = await svc.greet("y")
        assert result == "hello y"
        assert calls == ["before", "after_returning:hello y"]


# ---------------------------------------------------------------------------
# Non-matching methods
# ---------------------------------------------------------------------------


class TestNonMatchingMethods:
    @pytest.mark.asyncio
    async def test_non_matching_methods_untouched(self) -> None:
        calls: list[str] = []

        @aspect
        class NarrowAspect:
            @before("service.MyService.greet")
            def on_greet(self, jp):
                calls.append("hit")

        svc = MyService()
        registry = _make_registry(NarrowAspect())
        weave_bean(svc, "service.MyService", registry)

        # greet should be intercepted
        await svc.greet("x")
        assert calls == ["hit"]

        # explode should NOT be intercepted (no pointcut match)
        calls.clear()
        with pytest.raises(ValueError):
            await svc.explode()
        assert calls == []
