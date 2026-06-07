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
"""Autowiring feature-gap tests (v26.06.23): constructor @Value, Map injection,
Provider[T], and @lazy beans."""

from __future__ import annotations

from typing import Annotated

import pytest

from pyfly.container import Provider, lazy
from pyfly.container.container import Container
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.core.value import Value


# --- Constructor @Value injection ------------------------------------------
@pytest.mark.asyncio
async def test_constructor_value_injection() -> None:
    class Svc:
        def __init__(
            self,
            port: Annotated[int, Value("${app.port}")],
            name: Annotated[str, Value("${app.name}")],
        ) -> None:
            self.port = port
            self.name = name

    ctx = ApplicationContext(Config({"app": {"port": 9090, "name": "demo"}}))
    ctx.register_bean(Svc)
    await ctx.start()
    svc = ctx.get_bean(Svc)
    assert svc.port == 9090
    assert svc.name == "demo"


@pytest.mark.asyncio
async def test_constructor_value_default_is_coerced() -> None:
    class Svc:
        def __init__(self, port: Annotated[int, Value("${missing.port:8080}")]) -> None:
            self.port = port

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Svc)
    await ctx.start()
    svc = ctx.get_bean(Svc)
    assert svc.port == 8080
    assert isinstance(svc.port, int)  # the "8080" default string was coerced to int


# --- Map injection (dict[str, T]) ------------------------------------------
class _Handler:
    pass


class _A(_Handler):
    pass


class _B(_Handler):
    pass


def test_map_injection() -> None:
    c = Container()
    c.register(_A, name="a")
    c.register(_B, name="b")
    c.bind(_Handler, _A)
    c.bind(_Handler, _B)

    class Dispatcher:
        def __init__(self, handlers: dict[str, _Handler]) -> None:
            self.handlers = handlers

    c.register(Dispatcher)
    d = c.resolve(Dispatcher)
    assert set(d.handlers.keys()) == {"a", "b"}
    assert isinstance(d.handlers["a"], _A)
    assert isinstance(d.handlers["b"], _B)


# --- Provider[T] ------------------------------------------------------------
class _Job:
    pass


class _Worker:
    def __init__(self, jobs: Provider[_Job]) -> None:
        self.jobs = jobs


def test_provider_yields_fresh_transient_instances() -> None:
    c = Container()
    c.register(_Job, scope=Scope.TRANSIENT)
    c.register(_Worker)
    worker = c.resolve(_Worker)
    first = worker.jobs.get()
    second = worker.jobs.get()
    assert isinstance(first, _Job)
    assert first is not second  # fresh TRANSIENT instance each get()
    assert isinstance(worker.jobs(), _Job)  # callable shorthand


# --- @lazy beans ------------------------------------------------------------
@pytest.mark.asyncio
async def test_lazy_bean_is_not_eagerly_created() -> None:
    created: list[int] = []

    @lazy
    class Heavy:
        def __init__(self) -> None:
            created.append(1)

    ctx = ApplicationContext(Config({}))
    ctx.register_bean(Heavy)
    await ctx.start()
    assert created == []  # not built during startup

    ctx.get_bean(Heavy)
    assert created == [1]  # built on first resolution
