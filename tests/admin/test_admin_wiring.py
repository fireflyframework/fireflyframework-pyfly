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
"""build_admin_routes returns the admin dashboard routes for a started context."""

from __future__ import annotations

import pytest

from pyfly.admin.middleware.trace_collector import TraceCollectorFilter
from pyfly.admin.wiring import build_admin_routes
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


@pytest.mark.asyncio
async def test_build_admin_routes_returns_admin_routes() -> None:
    ctx = ApplicationContext(Config({"pyfly": {"admin": {"enabled": True}}}))
    await ctx.start()
    try:
        routes = build_admin_routes(
            ctx,
            admin_trace_collector=TraceCollectorFilter(),
            base_health_agg=None,
            extra_post_start=[],
        )
        paths = {getattr(r, "path", "") for r in routes}
        assert any(p.startswith("/admin") for p in paths)
    finally:
        await ctx.stop()
