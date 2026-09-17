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
"""An application adds routes to the management listener without touching the framework.

Two doors, both new in 26.09.05:

* :class:`ManagementRoutesContributor` — a bean returning Starlette routes that
  ``create_management_app`` mounts on the management port (and ``create_app`` on the main app
  when the management surface is shared), for an operation with a request body, its own
  authentication, or a shape the actuator's read model cannot carry.
* ``@write_operation`` — a method on a custom :class:`ActuatorEndpoint` mounted as
  ``POST /actuator/{id}`` with a JSON body, the Spring ``@WriteOperation`` equivalent, for the
  common case of a management command that takes a document.

Until these existed the only way to serve a POST with a body on the management port was to
replace ``pyfly.web.adapters.starlette.management_app.create_management_app`` on its module at
boot, from a bean whose only job was to exist early enough.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.actuator.ports import write_operation
from pyfly.container.stereotypes import component
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.management_app import create_management_app
from pyfly.web.ports.management import ManagementRoutesContributor


@component
class GuardrailDryRunRoutes:
    """The shape a service uses: an authenticated POST with a JSON body on the management port."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def management_routes(self) -> list[Route]:
        async def dry_run(request: Request) -> JSONResponse:
            if request.headers.get("authorization") != "Bearer service-token":
                return JSONResponse({"code": "SERVICE_TOKEN_REQUIRED"}, status_code=401)
            body = await request.json()
            self.calls.append(body)
            return JSONResponse({"verdict": "pass", "sample": body["sample"]})

        return [Route("/internal/guardrails/test", dry_run, methods=["POST"], name="guardrails.test")]


@component
class LevelEndpoint:
    """A custom actuator endpoint with a read and a write operation."""

    def __init__(self) -> None:
        self.level = "info"
        self.last_context: dict[str, Any] | None = None

    @property
    def endpoint_id(self) -> str:
        return "level"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        return {"level": self.level}

    @write_operation
    async def change(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
        self.last_context = context
        if body.get("level") not in ("debug", "info"):
            return {"error": f"unknown level {body.get('level')!r}"}
        self.level = body["level"]
        return None


@contextlib.asynccontextmanager
async def _noop_lifespan(app: Any):  # type: ignore[no-untyped-def]
    yield


def _exposure_config(**extra: Any) -> Config:
    return Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}, **extra}})


class TestManagementRoutesContributor:
    def test_the_protocol_is_structural(self) -> None:
        assert isinstance(GuardrailDryRunRoutes(), ManagementRoutesContributor)
        assert not isinstance(object(), ManagementRoutesContributor)

    async def test_routes_ride_on_the_management_app(self) -> None:
        ctx = ApplicationContext(_exposure_config())
        ctx.register_bean(GuardrailDryRunRoutes)
        await ctx.start()
        try:
            mgmt = create_management_app(
                ctx,
                health_agg=None,
                http_exchange_recorder=None,
                admin_trace_collector=None,
                actuator_active=True,
                admin_enabled=False,
                base_path="",
            )
            client = TestClient(mgmt)
            refused = client.post("/internal/guardrails/test", json={"sample": "x"})
            assert refused.status_code == 401
            answered = client.post(
                "/internal/guardrails/test",
                json={"sample": "hello"},
                headers={"authorization": "Bearer service-token"},
            )
            assert answered.status_code == 200
            assert answered.json() == {"verdict": "pass", "sample": "hello"}
            assert ctx.get_bean(GuardrailDryRunRoutes).calls == [{"sample": "hello"}]
            # The actuator is still there beside it.
            assert client.get("/actuator/health").status_code == 200
        finally:
            await ctx.stop()

    async def test_routes_ride_under_the_management_base_path(self) -> None:
        ctx = ApplicationContext(_exposure_config())
        ctx.register_bean(GuardrailDryRunRoutes)
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
            answered = client.post(
                "/manage/internal/guardrails/test",
                json={"sample": "hello"},
                headers={"authorization": "Bearer service-token"},
            )
            assert answered.status_code == 200
            assert client.post("/internal/guardrails/test", json={}).status_code == 404
        finally:
            await ctx.stop()

    async def test_routes_ride_on_the_main_app_when_the_management_surface_is_shared(self) -> None:
        """Shared mode means the main app IS the management surface, so the routes go there —
        an application that runs shared in tests and separate in production sees one behaviour."""
        ctx = ApplicationContext(_exposure_config())
        ctx.register_bean(GuardrailDryRunRoutes)
        await ctx.start()
        try:
            app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
            client = TestClient(app)
            answered = client.post(
                "/internal/guardrails/test",
                json={"sample": "hello"},
                headers={"authorization": "Bearer service-token"},
            )
            assert answered.status_code == 200
        finally:
            await ctx.stop()

    async def test_routes_leave_the_main_app_when_the_management_port_is_separate(self) -> None:
        ctx = ApplicationContext(_exposure_config(management={"server": {"port": 9099}}, server={"port": 8099}))
        ctx.register_bean(GuardrailDryRunRoutes)
        await ctx.start()
        try:
            app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False, lifespan=_noop_lifespan)
            paths = {getattr(r, "path", "") for r in app.router.routes}
            assert "/internal/guardrails/test" not in paths
        finally:
            await ctx.stop()

    async def test_routes_are_gone_when_management_is_disabled(self) -> None:
        ctx = ApplicationContext(_exposure_config(management={"server": {"port": -1}}))
        ctx.register_bean(GuardrailDryRunRoutes)
        await ctx.start()
        try:
            app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False, lifespan=_noop_lifespan)
            paths = {getattr(r, "path", "") for r in app.router.routes}
            assert "/internal/guardrails/test" not in paths
        finally:
            await ctx.stop()


class TestWriteOperation:
    def test_the_marker_is_on_the_function(self) -> None:
        assert getattr(LevelEndpoint.change, "__pyfly_write_operation__", False) is True

    def test_only_one_write_operation_per_endpoint(self) -> None:
        with pytest.raises(TypeError, match="one @write_operation"):

            class Twice:
                @property
                def endpoint_id(self) -> str:
                    return "twice"

                @property
                def enabled(self) -> bool:
                    return True

                async def handle(self, context: Any = None) -> dict[str, Any]:
                    return {}

                @write_operation
                async def first(self, body: dict[str, Any], context: dict[str, Any]) -> None:
                    return None

                @write_operation
                async def second(self, body: dict[str, Any], context: dict[str, Any]) -> None:
                    return None

            from pyfly.actuator.ports import find_write_operation

            find_write_operation(Twice())

    async def test_post_reaches_the_write_operation(self) -> None:
        ctx = ApplicationContext(_exposure_config())
        ctx.register_bean(LevelEndpoint)
        await ctx.start()
        try:
            app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
            client = TestClient(app)
            assert client.get("/actuator/level").json() == {"level": "info"}

            changed = client.post("/actuator/level?reason=incident", json={"level": "debug"})
            assert changed.status_code == 204
            assert client.get("/actuator/level").json() == {"level": "debug"}
            assert ctx.get_bean(LevelEndpoint).last_context == {"query": {"reason": "incident"}, "selector": None}

            refused = client.post("/actuator/level", json={"level": "loud"})
            assert refused.status_code == 400
            assert refused.json() == {"error": "unknown level 'loud'"}

            not_json = client.post("/actuator/level", content=b"{", headers={"content-type": "application/json"})
            assert not_json.status_code == 400
            assert not_json.json()["error"].startswith("request body is not a JSON object")

            not_an_object = client.post("/actuator/level", json=[1, 2])
            assert not_an_object.status_code == 400

            empty = client.post("/actuator/level")
            assert empty.status_code == 400
            assert client.get("/actuator/level").json() == {"level": "debug"}
        finally:
            await ctx.stop()

    async def test_an_endpoint_without_a_write_operation_refuses_post(self) -> None:
        @component
        class ReadOnly:
            @property
            def endpoint_id(self) -> str:
                return "readonly"

            @property
            def enabled(self) -> bool:
                return True

            async def handle(self, context: Any = None) -> dict[str, Any]:
                return {"ok": True}

        ctx = ApplicationContext(_exposure_config())
        ctx.register_bean(ReadOnly)
        await ctx.start()
        try:
            app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
            client = TestClient(app)
            assert client.get("/actuator/readonly").status_code == 200
            assert client.post("/actuator/readonly", json={}).status_code == 405
        finally:
            await ctx.stop()
