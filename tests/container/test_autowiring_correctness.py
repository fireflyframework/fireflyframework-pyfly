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
"""Regression tests for autowiring correctness fixes (v26.06.22).

1. @Qualifier verifies the named bean's type (no silent wrong-type injection).
2. list[T] / resolve_all honors @order.
3. @bean(profile=...) is profile-filtered.
4. The cycle-detection set is thread-local (no cross-thread false cycles).
5. @bean(primary=True) wins interface resolution.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Protocol, runtime_checkable

import pytest

from pyfly.container import bean, configuration
from pyfly.container.bean import Qualifier
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.ordering import order
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


# --- 1. @Qualifier type verification ---------------------------------------
class _A:
    pass


class _B:
    pass


def test_qualifier_with_wrong_type_raises_not_silent() -> None:
    c = Container()
    c.register(_A, name="myA")
    c.register(_B, name="myB")
    # bean 'myA' is an _A; asking for it as a _B must raise, not silently inject _A
    with pytest.raises(NoSuchBeanError):
        c.resolve_by_name("myA", expected_type=_B)
    assert isinstance(c.resolve_by_name("myA", expected_type=_A), _A)


def test_qualifier_param_wrong_type_raises() -> None:
    class Svc:
        def __init__(self, dep: Annotated[_B, Qualifier("myA")]) -> None:
            self.dep = dep

    c = Container()
    c.register(_A, name="myA")
    c.register(_B, name="myB")
    c.register(Svc)
    with pytest.raises(NoSuchBeanError):
        c.resolve(Svc)


# --- 2. resolve_all honors @order ------------------------------------------
class _Port:
    pass


@order(20)
class _Late(_Port):
    pass


@order(10)
class _Early(_Port):
    pass


def test_resolve_all_honors_order() -> None:
    c = Container()
    c.register(_Late)
    c.register(_Early)
    c.bind(_Port, _Late)  # bound in non-order order on purpose
    c.bind(_Port, _Early)
    names = [type(x).__name__ for x in c.resolve_all(_Port)]
    assert names == ["_Early", "_Late"]  # @order(10) before @order(20)


# --- 4. cycle-detection set is thread-local --------------------------------
def test_resolving_set_is_thread_local() -> None:
    c = Container()
    main_stack_id = id(c._resolving)
    with ThreadPoolExecutor(max_workers=1) as pool:
        other_stack_id = pool.submit(lambda: id(c._resolving)).result()
    assert main_stack_id != other_stack_id  # distinct per-thread cycle stacks


# --- 3 & 5. @bean profile + primary (via ApplicationContext) ---------------
@runtime_checkable
class _Greeter(Protocol):
    def hi(self) -> str: ...


class _En:
    def hi(self) -> str:
        return "hello"


class _Fr:
    def hi(self) -> str:
        return "bonjour"


@configuration
class _GreetCfg:
    @bean(primary=True)
    def en(self) -> _Greeter:
        return _En()

    @bean
    def fr(self) -> _Greeter:
        return _Fr()


@pytest.mark.asyncio
async def test_bean_level_primary_wins() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_GreetCfg)
    await ctx.start()
    assert ctx.get_bean(_Greeter).hi() == "hello"  # @bean(primary=True) chosen


@runtime_checkable
class _Notifier(Protocol):
    def send(self) -> str: ...


class _ProdNotifier:
    def send(self) -> str:
        return "prod"


@configuration
class _NotifyCfg:
    @bean(profile="prod")
    def notifier(self) -> _Notifier:
        return _ProdNotifier()


@pytest.mark.asyncio
async def test_bean_profile_created_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "prod")
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_NotifyCfg)
    await ctx.start()
    assert ctx.get_bean(_Notifier).send() == "prod"


@pytest.mark.asyncio
async def test_bean_profile_skipped_when_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFLY_PROFILES_ACTIVE", "dev")
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_NotifyCfg)
    await ctx.start()
    with pytest.raises(NoSuchBeanError):
        ctx.get_bean(_Notifier)
