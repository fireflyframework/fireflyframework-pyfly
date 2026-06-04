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
"""Wave-0 web-wiring keystone (audit #40 / #163).

create_app() builds the filter chain and collects routes BEFORE
ApplicationContext.start(), so beans only instantiated during startup
(security/session WebFilters, @bean-produced controllers) used to be silently
dropped. The lifespan now re-runs filter/route discovery after start().
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.mappings import get_mapping, request_mapping


@request_mapping("/api/late")
class LateController:
    """Plain class mounted via a @bean factory (registered only during start)."""

    @get_mapping("/ping")
    async def ping(self) -> dict:
        return {"pong": True}


# @rest_controller stereotype is applied via the bean's class metadata; mark it
# so ControllerRegistrar recognises it.
LateController.__pyfly_stereotype__ = "controller"  # type: ignore[attr-defined]


class LateHeaderFilter(OncePerRequestFilter):
    async def do_filter(self, request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Late-Filter"] = "on"
        return response


@configuration
class LateWiringConfig:
    @bean
    def late_controller(self) -> LateController:
        return LateController()

    @bean
    def late_filter(self) -> LateHeaderFilter:
        return LateHeaderFilter()


def _build_app() -> tuple[Any, ApplicationContext]:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(LateWiringConfig)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()  # registers LateController class + builds LateHeaderFilter
        yield
        await ctx.stop()

    app = create_app(context=ctx, lifespan=_lifespan)
    return app, ctx


@pytest.mark.asyncio
async def test_bean_produced_controller_reachable_after_start() -> None:
    app, _ctx = _build_app()
    with TestClient(app) as client:  # triggers lifespan startup
        resp = client.get("/api/late/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}


@pytest.mark.asyncio
async def test_late_filter_bean_joins_chain_after_start() -> None:
    app, _ctx = _build_app()
    with TestClient(app) as client:
        resp = client.get("/api/late/ping")
        assert resp.headers.get("X-Late-Filter") == "on"
