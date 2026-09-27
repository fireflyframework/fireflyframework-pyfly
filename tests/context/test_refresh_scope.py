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
"""@RefreshScope + ContextRefresher (v26.06.52)."""

from __future__ import annotations

import pytest

from pyfly.container.container import Container
from pyfly.container.refresh_scope import RefreshScope, refresh_scope
from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import ApplicationEventBus, RefreshScopeRefreshedEvent, app_event_listener
from pyfly.context.lifecycle import pre_destroy
from pyfly.context.refresh import ContextRefresher
from pyfly.core.config import Config


def test_refresh_scope_handler_caches_and_evicts() -> None:
    scope = RefreshScope()
    calls = {"n": 0}

    def factory() -> int:
        calls["n"] += 1
        return calls["n"]

    assert scope.get("k", factory) == 1
    assert scope.get("k", factory) == 1  # cached
    assert scope.refresh() == ["k"]
    assert scope.get("k", factory) == 2  # rebuilt after refresh
    assert scope.remove("k") == 2
    assert scope.remove("absent") is None


@refresh_scope
class _Counter:
    _n = 0

    def __init__(self) -> None:
        type(self)._n += 1
        self.id = type(self)._n


_refresh_events: list[list[str]] = []


@service
class _Listener:
    @app_event_listener
    async def on_refresh(self, event: RefreshScopeRefreshedEvent) -> None:
        _refresh_events.append(event.refreshed)


@pytest.mark.asyncio
async def test_refresh_scoped_bean_rebuilds_on_refresh() -> None:
    _Counter._n = 0
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Counter)
    await ctx.start()

    first = ctx.get_bean(_Counter)
    assert ctx.get_bean(_Counter) is first  # cached within the refresh scope

    refresher = ctx.get_bean(ContextRefresher)
    assert isinstance(refresher, ContextRefresher)
    evicted = await refresher.refresh()
    assert evicted  # the counter's cache key was evicted

    rebuilt = ctx.get_bean(_Counter)
    assert rebuilt is not first
    assert rebuilt.id == first.id + 1


@pytest.mark.asyncio
async def test_refresh_publishes_event() -> None:
    _refresh_events.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Listener)
    await ctx.start()

    await ctx.get_bean(ContextRefresher).refresh()
    assert _refresh_events  # listener received RefreshScopeRefreshedEvent


@pytest.mark.asyncio
async def test_actuator_post_refresh_endpoint() -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
    from pyfly.actuator.endpoints.refresh_endpoint import RefreshEndpoint
    from pyfly.actuator.registry import ActuatorRegistry

    _Counter._n = 0
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_Counter)
    await ctx.start()
    ctx.get_bean(_Counter)  # populate the refresh scope

    registry = ActuatorRegistry()
    registry.register(RefreshEndpoint(ctx))
    client = TestClient(Starlette(routes=make_starlette_actuator_routes(registry)))

    resp = client.post("/actuator/refresh")
    assert resp.status_code == 200
    assert resp.json()["refreshed"]  # the counter's cache key was refreshed
    # the bean rebuilds after the HTTP-triggered refresh
    assert ctx.get_bean(_Counter).id == 2


class _Closing:
    closed: list[str] = []

    @pre_destroy
    async def close(self) -> None:
        type(self).closed.append("close")

    @pre_destroy
    def release(self) -> None:
        type(self).closed.append("release")

    @property
    def not_a_hook(self) -> str:
        raise AssertionError("a property is never evaluated while looking for @pre_destroy")


async def test_a_refresher_without_a_destroy_callback_runs_the_pre_destroy_methods() -> None:
    """Without the context's destroy callback, the refresher calls the evicted instance's ``@pre_destroy``."""
    _Closing.closed = []
    container = Container()
    scope = RefreshScope()
    scope.get("closing", _Closing)
    refresher = ContextRefresher(container, scope, ApplicationEventBus())

    assert await refresher.refresh() == ["closing"]
    assert sorted(_Closing.closed) == ["close", "release"]
