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
"""Regressions caught by the final parity audit (v26.06.32):

1. @bean(primary=True) must win when multiple @bean methods share a return type
   (the direct return-type registration previously kept the first-processed impl).
2. @lazy beans must run the full init pipeline (BeanPostProcessors, @post_construct)
   on first resolution — previously lazy beans skipped it entirely.
"""

from __future__ import annotations

import abc
from typing import Any

import pytest

from pyfly.container import bean, configuration, lazy
from pyfly.context.application_context import ApplicationContext
from pyfly.context.lifecycle import post_construct
from pyfly.context.post_processor import BeanPostProcessor
from pyfly.core.config import Config


# --- 1. @bean(primary) across multiple @bean methods of one interface -------
class _Payment(abc.ABC):
    @abc.abstractmethod
    def pay(self) -> str: ...


class _Paypal(_Payment):
    def pay(self) -> str:
        return "paypal"


class _Stripe(_Payment):
    def pay(self) -> str:
        return "stripe"


@configuration
class _PaymentConfig:
    @bean
    def paypal(self) -> _Payment:
        return _Paypal()

    @bean(primary=True)
    def stripe(self) -> _Payment:
        return _Stripe()


@pytest.mark.asyncio
async def test_bean_primary_wins_for_shared_return_type() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_PaymentConfig)
    await ctx.start()
    assert ctx.get_bean(_Payment).pay() == "stripe"  # the @bean(primary=True), not first-processed


# --- 2. @lazy beans run the full init pipeline on first resolution ----------
@lazy
class _LazyWithPostConstruct:
    def __init__(self) -> None:
        self.initialized = False

    @post_construct
    def _init(self) -> None:
        self.initialized = True


@pytest.mark.asyncio
async def test_lazy_bean_runs_post_construct_on_first_resolve() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_LazyWithPostConstruct)
    await ctx.start()
    bean_obj = ctx.get_bean(_LazyWithPostConstruct)
    assert bean_obj.initialized is True  # @post_construct ran at lazy creation


class _Marking(BeanPostProcessor):
    def before_init(self, bean: Any, name: str) -> Any:
        if hasattr(bean, "marks"):
            bean.marks.append("before")
        return bean

    def after_init(self, bean: Any, name: str) -> Any:
        if hasattr(bean, "marks"):
            bean.marks.append("after")
        return bean


@lazy
class _LazyMarked:
    def __init__(self) -> None:
        self.marks: list[str] = []


@pytest.mark.asyncio
async def test_lazy_bean_runs_bean_post_processors_on_first_resolve() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_post_processor(_Marking())
    ctx.register_bean(_LazyMarked)
    await ctx.start()
    assert ctx.get_bean(_LazyMarked).marks == [
        "before",
        "after",
    ]  # BeanPostProcessors ran (AOP weaving uses the same path)
