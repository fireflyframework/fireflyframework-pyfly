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

import contextlib
from typing import Any

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


@contextlib.asynccontextmanager
async def _noop_lifespan(app: Any):
    # A lifespan is required for separation to take effect (it starts the
    # management listener); a no-op one is enough to exercise the route gating.
    yield


async def _main_paths(config_dict: dict, *, lifespan: object | None = _noop_lifespan) -> set[str]:
    ctx = ApplicationContext(Config(config_dict))
    await ctx.start()
    try:
        app = create_app(context=ctx, docs_enabled=False, lifespan=lifespan)
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
    paths = await _main_paths({"pyfly": {"management": {"server": {"port": -1}}, "admin": {"enabled": True}}})
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
        fa = create_fastapi_app(context=ctx, docs_enabled=False, lifespan=_noop_lifespan)
        fa.state.pyfly_install_dynamic_wiring()
        paths = {getattr(r, "path", "") for r in fa.routes}
        assert not any(p.startswith("/actuator") for p in paths)
        assert not any(p.startswith("/admin") for p in paths)
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_separate_mode_without_lifespan_degrades_to_shared() -> None:
    # No lifespan means the management listener cannot start, so actuator/admin
    # must remain on the main app instead of silently vanishing.
    paths = await _main_paths(
        {
            "pyfly": {
                "server": {"port": 8080},
                "management": {"server": {"port": 9097}, "endpoints": {"web": {"exposure": {"include": "*"}}}},
                "admin": {"enabled": True},
            }
        },
        lifespan=None,
    )
    assert any(p.startswith("/actuator") for p in paths)
    assert any(p.startswith("/admin") for p in paths)


@pytest.mark.asyncio
async def test_health_aggregator_exposed_and_live_in_shared_mode() -> None:
    # app.state.pyfly_health_aggregator must be the SAME aggregator backing the
    # live health routes: an indicator added to it post-create_app shows up on
    # /actuator/health (the mechanism cdm-mexico uses for its Fabric readiness probe).
    from starlette.testclient import TestClient

    from pyfly.actuator.health import HealthAggregator, HealthStatus, ProbeGroup

    class _DownDep:
        async def health(self) -> HealthStatus:
            return HealthStatus(status="DOWN", details={"reason": "offline"})

    ctx = ApplicationContext(
        Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "health"}}}}}})
    )

    @contextlib.asynccontextmanager
    async def _lifespan(app_: Any):
        await ctx.start()
        app_.state.pyfly_install_dynamic_wiring()
        yield
        await ctx.stop()

    app = create_app(context=ctx, docs_enabled=False, lifespan=_lifespan)
    agg = getattr(app.state, "pyfly_health_aggregator", None)
    assert isinstance(agg, HealthAggregator)

    with TestClient(app) as client:
        assert client.get("/actuator/health/readiness").status_code == 200
        # Register a readiness-only DOWN indicator on the exposed aggregator.
        agg.add_indicator("dep", _DownDep(), groups={ProbeGroup.READINESS})
        readiness = client.get("/actuator/health/readiness")
        assert readiness.status_code == 503
        assert readiness.json()["components"]["dep"]["status"] == "DOWN"
        # Readiness-only: liveness stays UP (the probe-group semantics hold).
        assert client.get("/actuator/health/liveness").status_code == 200


def test_management_port_open_by_default_ignores_user_security_filters() -> None:
    # The management app must NOT apply the app's user security filters by default
    # (a deny-all HttpSecurity gate scoped to the main app would 403 /admin etc.).
    import asyncio

    from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
    from pyfly.web.adapters.starlette.management_app import create_management_app
    from pyfly.web.filters import OncePerRequestFilter
    from pyfly.web.ports.filter import WebFilter

    @order(HIGHEST_PRECEDENCE + 350)
    class _DenyAll(OncePerRequestFilter):
        async def do_filter(self, request, call_next):  # type: ignore[no-untyped-def]
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("denied", status_code=403)

    async def _mgmt_filter_names(config: dict) -> list[str]:
        ctx = ApplicationContext(Config(config))
        ctx.container.register_instance(WebFilter, _DenyAll(), name="deny_all")
        await ctx.start()
        try:
            app = create_management_app(
                ctx,
                health_agg=None,
                http_exchange_recorder=None,
                admin_trace_collector=None,
                actuator_active=True,
                admin_enabled=False,
            )
            return [
                type(f).__name__
                for mw in app.user_middleware
                for f in (getattr(mw, "kwargs", {}) or {}).get("filters", []) or []
            ]
        finally:
            await ctx.stop()

    # Default: the deny-all gate is NOT on the management app → actuator/admin open.
    assert "_DenyAll" not in asyncio.run(_mgmt_filter_names({"pyfly": {}}))
    # Opt in: pyfly.management.security.enabled=true applies it.
    assert "_DenyAll" in asyncio.run(_mgmt_filter_names({"pyfly": {"management": {"security": {"enabled": "true"}}}}))


@pytest.mark.asyncio
async def test_health_aggregator_exposed_in_separate_mode() -> None:
    # Even when actuator runs on a separate management port, the aggregator is
    # exposed on the main app's state (it is the same instance the management app
    # serves), so consumers holding the main app can still register indicators.
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "server": {"port": 8080},
                    "management": {"server": {"port": 9096}, "endpoints": {"web": {"exposure": {"include": "*"}}}},
                }
            }
        )
    )
    await ctx.start()
    try:
        from pyfly.actuator.health import HealthAggregator

        # No lifespan entry → the management listener never binds a real port.
        app = create_app(context=ctx, docs_enabled=False, lifespan=_noop_lifespan)
        assert isinstance(getattr(app.state, "pyfly_health_aggregator", None), HealthAggregator)
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
