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
"""When a separate management port is configured, the MAIN app omits actuator/admin.

Tests build raw-dict ``Config`` objects (which do NOT load pyfly-defaults.yaml),
so the management port is whatever the test sets — unset means shared.
"""

from __future__ import annotations

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


async def _main_paths(config_dict: dict) -> set[str]:
    ctx = ApplicationContext(Config(config_dict))
    await ctx.start()
    try:
        app = create_app(context=ctx, docs_enabled=False)
        # Run the post-start rescan the lifespan normally performs.
        app.state.pyfly_install_dynamic_wiring()
        return {getattr(r, "path", "") for r in app.router.routes}
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_shared_mode_keeps_actuator_and_admin_on_main() -> None:
    paths = await _main_paths(
        {"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}, "admin": {"enabled": True}}}
    )
    assert any(p.startswith("/actuator") for p in paths)
    assert any(p.startswith("/admin") for p in paths)


@pytest.mark.asyncio
async def test_separate_mode_removes_actuator_and_admin_from_main() -> None:
    paths = await _main_paths(
        {
            "pyfly": {
                "server": {"port": 8080},
                "management": {"server": {"port": 9099}, "endpoints": {"web": {"exposure": {"include": "*"}}}},
                "admin": {"enabled": True},
            }
        }
    )
    assert not any(p.startswith("/actuator") for p in paths)
    assert not any(p.startswith("/admin") for p in paths)


@pytest.mark.asyncio
async def test_disabled_mode_removes_actuator_and_admin_everywhere() -> None:
    paths = await _main_paths(
        {"pyfly": {"management": {"server": {"port": -1}}, "admin": {"enabled": True}}}
    )
    assert not any(p.startswith("/actuator") for p in paths)
    assert not any(p.startswith("/admin") for p in paths)


@pytest.mark.asyncio
async def test_separate_mode_fastapi_parity() -> None:
    from importlib.util import find_spec

    if find_spec("fastapi") is None:  # pragma: no cover
        pytest.skip("fastapi not installed")

    from pyfly.web.adapters.fastapi.app import create_app as create_fastapi_app

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "server": {"port": 8080},
                    "management": {"server": {"port": 9098}, "endpoints": {"web": {"exposure": {"include": "*"}}}},
                    "admin": {"enabled": True},
                }
            }
        )
    )
    await ctx.start()
    try:
        fa = create_fastapi_app(context=ctx, docs_enabled=False)
        fa.state.pyfly_install_dynamic_wiring()
        paths = {getattr(r, "path", "") for r in fa.routes}
        assert not any(p.startswith("/actuator") for p in paths)
        assert not any(p.startswith("/admin") for p in paths)
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_equal_port_stays_shared() -> None:
    paths = await _main_paths(
        {
            "pyfly": {
                "server": {"port": 8080},
                "management": {"server": {"port": 8080}, "endpoints": {"web": {"exposure": {"include": "*"}}}},
                "admin": {"enabled": True},
            }
        }
    )
    assert any(p.startswith("/actuator") for p in paths)
    assert any(p.startswith("/admin") for p in paths)
