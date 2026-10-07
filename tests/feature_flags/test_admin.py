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
"""Feature flag admin API through the actual application routes."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app

FLAGS = {
    "kill": True,
    "theme": {"state": "ENABLED", "variants": {"light": "light", "dark": "dark"}, "defaultVariant": "light"},
}
WRITABLE = {"sources": {"store": {"enabled": True, "driver": "memory"}}, "management": {"writes": True}}


def _config(feature_flags: dict[str, Any] | None = None, **admin: Any) -> Config:
    section = {"enabled": "true", "flags": FLAGS, **(feature_flags or {})}
    return Config({"pyfly": {"admin": {"enabled": True, **admin}, "feature-flags": section}})


@contextlib.asynccontextmanager
async def _client(config: Config) -> AsyncIterator[httpx.AsyncClient]:
    context = ApplicationContext(config)
    await context.start()
    app = create_app(context=context, actuator_enabled=False, docs_enabled=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        await context.stop()


async def test_admin_lists_shows_previews_writes_and_reports_errors() -> None:
    async with _client(_config(WRITABLE)) as client:
        listing = await client.get("/admin/api/flags")
        assert listing.status_code == 200
        assert [flag["key"] for flag in listing.json()["flags"]] == ["kill", "theme"]
        assert (await client.get("/admin/api/flags/theme")).json()["origin"] == "config"
        preview = await client.post("/admin/api/flags/theme", json={"action": "evaluate"})
        assert preview.json()["variant"] == "light"
        changed = await client.post("/admin/api/flags/kill", json={"action": "disable"})
        assert changed.json()["history"][0]["actor"] == "admin"
        conflict = await client.post("/admin/api/flags/kill", json={"action": "enable", "expectedVersion": 0})
        assert (conflict.status_code, conflict.json()["error"]) == (409, "conflict")
        invalid = await client.post("/admin/api/flags/kill", json={"action": "put", "definition": {"state": "ON"}})
        assert (invalid.status_code, invalid.json()["error"]) == (422, "invalid-definition")
        unknown = await client.get("/admin/api/flags/missing")
        assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown-flag")
        bad = await client.post("/admin/api/flags/kill", content=b"not json")
        assert (bad.status_code, bad.json()["error"]) == (400, "bad-request")


async def test_admin_write_switch_and_unavailable_feature() -> None:
    async with _client(_config()) as client:
        denied = await client.post("/admin/api/flags/kill", json={"action": "disable"})
        assert (denied.status_code, denied.json()["error"]) == (403, "writes-disabled")
    async with _client(Config({"pyfly": {"admin": {"enabled": True}}})) as client:
        assert (await client.get("/admin/api/flags")).json() == {"available": False}
        denied = await client.post("/admin/api/flags/kill", json={"action": "disable"})
        assert (denied.status_code, denied.json()["error"]) == (409, "not-writable")


async def test_admin_flags_routes_are_guarded() -> None:
    async with _client(_config(WRITABLE, **{"require-auth": True})) as client:
        assert (await client.get("/admin/api/flags")).status_code == 401
        assert (await client.post("/admin/api/flags/kill", json={"action": "disable"})).status_code == 401


async def test_admin_config_views_survive_unresolved_flag_placeholders() -> None:
    flags = {"message": {"state": "ENABLED", "variants": {"hello": "Hello ${name}"}, "defaultVariant": "hello"}}
    async with _client(_config({"flags": flags})) as client:
        config = await client.get("/admin/api/config")
        env = await client.get("/admin/api/env")
        assert config.status_code == 200 and env.status_code == 200
        flag_value = config.json()["groups"]["pyfly.feature-flags"]["flags.message.variants.hello"]["value"]
        assert flag_value == "Hello ${name}"


async def test_admin_flag_route_works_when_routes_precede_context_start() -> None:
    context = ApplicationContext(_config(WRITABLE))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = create_app(context=context, lifespan=lifespan, actuator_enabled=False, docs_enabled=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        listing = await client.get("/admin/api/flags")
        assert listing.json()["available"] is True
        view = await client.get("/admin/static/js/views/flags.js")
        assert view.status_code == 200
