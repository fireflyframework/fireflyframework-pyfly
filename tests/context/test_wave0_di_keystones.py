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
"""Wave-0 DI keystone regression tests (audit findings #113, #115, #116, #119).

These guard the container/context fixes that unblock the rest of the
remediation: interface-typed @bean double-registration, synchronous
@app_event_listener support, TRANSIENT @bean factory dispatch, and
app-event-type inference ignoring the return annotation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pytest

from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration, service
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import ApplicationReadyEvent, app_event_listener
from pyfly.context.lifecycle import post_construct
from pyfly.core.config import Config

# --- #113: interface-typed @bean must be processed exactly once ---


class Greeter(ABC):
    @abstractmethod
    def greet(self) -> str: ...


_POST_CONSTRUCT_CALLS: list[int] = []


class RealGreeter(Greeter):
    @post_construct
    def _init(self) -> None:
        _POST_CONSTRUCT_CALLS.append(id(self))

    def greet(self) -> str:
        return "hi"


@configuration
class GreeterConfig:
    @bean
    def greeter(self) -> Greeter:
        return RealGreeter()


class _CountingPostProcessor:
    """Records how many times each bean instance is passed through init hooks."""

    def __init__(self) -> None:
        self.before: dict[int, int] = {}
        self.after: dict[int, int] = {}

    def before_init(self, bean: Any, bean_name: str) -> Any:
        self.before[id(bean)] = self.before.get(id(bean), 0) + 1
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        self.after[id(bean)] = self.after.get(id(bean), 0) + 1
        return bean


@pytest.mark.asyncio
async def test_interface_bean_post_construct_runs_once() -> None:
    _POST_CONSTRUCT_CALLS.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(GreeterConfig)
    await ctx.start()

    assert len(_POST_CONSTRUCT_CALLS) == 1, "@post_construct must run once, not per registration"


@pytest.mark.asyncio
async def test_interface_bean_post_processors_run_once() -> None:
    pp = _CountingPostProcessor()
    ctx = ApplicationContext(Config({}))
    ctx.register_post_processor(pp)
    ctx.register_bean(GreeterConfig)
    await ctx.start()

    greeter = ctx.get_bean(Greeter)
    assert pp.before.get(id(greeter)) == 1, "before_init must run once per instance"
    assert pp.after.get(id(greeter)) == 1, "after_init must run once per instance"


@pytest.mark.asyncio
async def test_interface_and_concrete_resolve_to_same_instance() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(GreeterConfig)
    await ctx.start()

    # Both the interface key and the concrete key must yield the identical
    # (post-processed) object, so AOP/BPP replacements are not lost on aliases.
    assert ctx.get_bean(Greeter) is ctx.get_bean(RealGreeter)


# --- #115: synchronous @app_event_listener must not crash startup ---

_SYNC_LISTENER_HITS: list[str] = []


@service
class SyncReadyListener:
    @app_event_listener
    def on_ready(self, event: ApplicationReadyEvent) -> None:  # sync, returns None
        _SYNC_LISTENER_HITS.append("ready")


@pytest.mark.asyncio
async def test_sync_app_event_listener_does_not_crash_startup() -> None:
    _SYNC_LISTENER_HITS.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(SyncReadyListener)
    await ctx.start()  # must not raise "object NoneType can't be used in 'await'"

    assert _SYNC_LISTENER_HITS == ["ready"]


# --- #116: TRANSIENT @bean must dispatch through the factory each resolution ---


class Widget:
    def __init__(self, made_by: str = "init") -> None:
        self.made_by = made_by


@configuration
class WidgetConfig:
    @bean(scope=Scope.TRANSIENT)
    def widget(self) -> Widget:
        return Widget(made_by="factory")


@pytest.mark.asyncio
async def test_transient_bean_uses_factory_each_resolution() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(WidgetConfig)
    await ctx.start()

    w1 = ctx.get_bean(Widget)
    w2 = ctx.get_bean(Widget)
    assert w1.made_by == "factory", "factory logic must be used, not __init__"
    assert w2.made_by == "factory"
    assert w1 is not w2, "TRANSIENT must produce a new instance each resolution"


# --- #119: app-event-type inference must ignore the return annotation ---

_INFER_HITS: list[str] = []


@service
class ReturnAnnotatedListener:
    @app_event_listener
    def handle(self, event: Any) -> ApplicationReadyEvent:  # type: ignore[empty-body]
        _INFER_HITS.append(type(event).__name__)
        return event  # pragma: no cover - return value unused by the bus


@pytest.mark.asyncio
async def test_event_inference_ignores_return_annotation() -> None:
    _INFER_HITS.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(ReturnAnnotatedListener)
    await ctx.start()

    # The return annotation (ApplicationReadyEvent) must NOT make this a
    # Ready-only listener; with an untyped event param it falls back to the
    # catch-all ApplicationEvent and therefore also sees ContextRefreshedEvent.
    assert "ContextRefreshedEvent" in _INFER_HITS


# --- #117: conditional_on_property match_if_missing + false opt-out ---


def _eval(cfg: dict, **cond_kwargs) -> bool:
    from pyfly.context.condition_evaluator import ConditionEvaluator

    ev = ConditionEvaluator(Config(cfg), ApplicationContext(Config({}))._container)
    cond = {"type": "on_property", "key": "feature.enabled", "having_value": "", **cond_kwargs}
    return ev._eval_on_property(cond)


def test_conditional_on_property_match_if_missing() -> None:
    assert _eval({}, match_if_missing=True) is True
    assert _eval({}, match_if_missing=False) is False


def test_conditional_on_property_false_is_opt_out() -> None:
    # Present-and-not-"false" matches; explicit false does not.
    assert _eval({"feature": {"enabled": "true"}}) is True
    assert _eval({"feature": {"enabled": "false"}}) is False
    assert _eval({"feature": {"enabled": False}}) is False
