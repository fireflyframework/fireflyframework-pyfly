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
"""Regression: health indicators instantiated during the ASGI lifespan startup
must be reflected by /actuator/health (the rescan hook must actually run)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.testclient import TestClient

from pyfly.actuator.health import HealthStatus
from pyfly.container.stereotypes import component
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


@component
class _DownIndicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="DOWN", details={"reason": "dependency offline"})


def test_actuator_health_reflects_indicator_registered_at_startup():
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(_DownIndicator)

    # The context is started INSIDE the lifespan — i.e. after create_app returns,
    # exactly like the generated main.py. The eager scan at create_app time
    # therefore cannot see _DownIndicator; only the post-startup rescan can.
    @asynccontextmanager
    async def lifespan(app):
        await ctx.start()
        yield

    app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False, lifespan=lifespan)

    with TestClient(app) as client:  # entering the client triggers the lifespan
        resp = client.get("/actuator/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "DOWN"
        assert any(c.get("status") == "DOWN" for c in body.get("components", {}).values())
