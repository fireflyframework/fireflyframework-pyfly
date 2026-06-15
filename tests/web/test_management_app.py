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
"""create_management_app builds an actuator + admin-only Starlette app."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.management_app import create_management_app


@pytest.mark.asyncio
async def test_management_app_serves_actuator_and_admin_not_business() -> None:
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "management": {"endpoints": {"web": {"exposure": {"include": "*"}}}},
                    "admin": {"enabled": True},
                }
            }
        )
    )
    await ctx.start()
    try:
        mgmt = create_management_app(
            ctx,
            health_agg=None,
            http_exchange_recorder=None,
            admin_trace_collector=None,
            actuator_active=True,
            admin_enabled=True,
            base_path="",
        )
        client = TestClient(mgmt)
        assert client.get("/actuator/health").status_code == 200
        # admin dashboard SPA shell is mounted
        assert client.get("/admin/").status_code in (200, 307, 308)
        # business paths do not exist on the management app
        assert client.get("/does-not-exist").status_code == 404
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_management_app_base_path_prefix() -> None:
    ctx = ApplicationContext(
        Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}}})
    )
    await ctx.start()
    try:
        mgmt = create_management_app(
            ctx,
            health_agg=None,
            http_exchange_recorder=None,
            admin_trace_collector=None,
            actuator_active=True,
            admin_enabled=False,
            base_path="/manage",
        )
        client = TestClient(mgmt)
        assert client.get("/manage/actuator/health").status_code == 200
        assert client.get("/actuator/health").status_code == 404
    finally:
        await ctx.stop()
