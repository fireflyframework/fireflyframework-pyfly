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
"""The flags actuator, health component, and configprops binding."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.management import FlagManagement
from pyfly.web.adapters.starlette.app import create_app

EVERYTHING = {"web": {"exposure": {"include": "*"}}}
FLAGS = {"kill": True, "theme": "dark"}
WRITABLE = {"sources": {"store": {"enabled": True, "driver": "memory"}}, "management": {"writes": True}}


def _config(feature_flags: dict[str, Any] | None = None, management: dict[str, Any] | None = None) -> Config:
    section = {"enabled": "true", "flags": FLAGS, **(feature_flags or {})}
    return Config({"pyfly": {"management": {"endpoints": EVERYTHING, **(management or {})}, "feature-flags": section}})


@contextlib.asynccontextmanager
async def _client(config: Config, *beans: type) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContext]]:
    context = ApplicationContext(config)
    for bean_class in beans:
        context.register_bean(bean_class)
    await context.start()
    app = create_app(context=context, actuator_enabled=True, docs_enabled=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, context
    finally:
        await context.stop()


async def test_the_list_and_the_detail() -> None:
    async with _client(_config(WRITABLE)) as (client, _):
        listing = (await client.get("/actuator/flags")).json()
        assert listing["provider"] == {"name": "firefly", "status": "READY"}
        assert (listing["writable"], listing["writesEnabled"]) == (True, True)
        assert [flag["key"] for flag in listing["flags"]] == ["kill", "theme"]
        detail = (await client.get("/actuator/flags/theme")).json()
        assert detail["origin"] == "config" and detail["definition"]["defaultVariant"] == "dark"
        assert (await client.get("/actuator/flags/missing")).status_code == 404


async def test_post_runs_the_actions_and_records_the_actor() -> None:
    async with _client(_config(WRITABLE)) as (client, _):
        response = await client.post("/actuator/flags/kill", json={"action": "disable"})
        assert response.status_code == 200
        assert response.json()["definition"]["state"] == "DISABLED"
        assert response.json()["history"][0]["actor"] == "actuator"
        evaluated = await client.post("/actuator/flags/theme", json={"action": "evaluate"})
        assert evaluated.json()["value"] == "dark"


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("/actuator/flags/kill", {"action": "default-variant", "variant": "nope"}, "unknown-variant"),
        ("/actuator/flags/missing", {"action": "enable"}, "unknown-flag"),
        ("/actuator/flags/kill", {"action": "teleport"}, "bad-request"),
        ("/actuator/flags", {"action": "enable"}, "bad-request"),
    ],
)
async def test_errors_answer_400_with_the_portable_code(path: str, body: dict[str, Any], code: str) -> None:
    async with _client(_config(WRITABLE)) as (client, _):
        response = await client.post(path, json=body)
    assert response.status_code == 400
    assert response.json()["error"] == code and response.json()["message"]


async def test_writes_are_refused_without_the_switch() -> None:
    async with _client(_config()) as (client, _):
        response = await client.post("/actuator/flags/kill", json={"action": "disable"})
    assert (response.status_code, response.json()["error"]) == (400, "writes-disabled")


async def test_post_preserves_a_pending_refresh_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_config(WRITABLE)) as (client, context):
        management = context.container.resolve(FlagManagement)
        assert management.registry is not None
        source = management.registry._by_name["store"].source

        async def fail_load() -> None:
            raise RuntimeError("controlled refresh failure")

        monkeypatch.setattr(source, "load", fail_load)
        response = await client.post(
            "/actuator/flags/new",
            json={
                "action": "put",
                "definition": {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "on"},
            },
        )
    assert response.status_code == 200
    assert response.json() == {"key": "new", "refreshPending": True}


async def test_the_endpoint_follows_the_exposure_and_enable_switches() -> None:
    hidden = Config({"pyfly": {"feature-flags": {"enabled": "true", "flags": FLAGS}}})
    async with _client(hidden) as (client, _):
        assert (await client.get("/actuator/flags")).status_code == 404
    disabled = _config(management={"endpoint": {"flags": {"enabled": False}}})
    async with _client(disabled) as (client, _):
        assert (await client.get("/actuator/flags")).status_code == 404


async def test_the_flags_endpoint_answers_under_the_canonical_boot_order() -> None:
    context = ApplicationContext(_config(WRITABLE))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = create_app(context=context, lifespan=lifespan, actuator_enabled=True, docs_enabled=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert [f["key"] for f in (await client.get("/actuator/flags")).json()["flags"]] == ["kill", "theme"]
        assert (await client.post("/actuator/flags/kill", json={"action": "disable"})).status_code == 200


async def test_the_health_component_reports_sources_and_expired_flags() -> None:
    flags = {
        **FLAGS,
        "old": {"state": "ENABLED", "variants": {"a": 1}, "defaultVariant": "a", "metadata": {"expires": "2020-01-01"}},
    }
    async with _client(_config({"flags": flags})) as (client, _):
        health = (await client.get("/actuator/health")).json()
    component = health["components"]["featureFlags"]
    assert component["status"] == "UP"
    assert component["details"]["provider"] == "firefly" and component["details"]["flags"] == 3
    assert component["details"]["expired"] == ["old"]
    assert component["details"]["sources"]["config"]["status"] == "UP"


async def test_a_source_that_never_loaded_makes_the_component_down() -> None:
    http = {"enabled": True, "url": "http://127.0.0.1:9/flagd.json", "timeout": "200ms"}
    async with _client(_config({"sources": {"http": http}})) as (client, _):
        response = await client.get("/actuator/health")
    assert response.status_code == 503
    assert response.json()["components"]["featureFlags"]["status"] == "DOWN"


async def test_a_stale_source_with_last_good_flags_keeps_the_component_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(_config()) as (client, context):
        management = context.container.resolve(FlagManagement)
        assert management.registry is not None
        source = management.registry._by_name["config"].source

        async def fail_load() -> None:
            raise RuntimeError("controlled refresh failure")

        monkeypatch.setattr(source, "load", fail_load)
        await management.registry.refresh("config")
        response = await client.get("/actuator/health")
    component = response.json()["components"]["featureFlags"]
    assert response.status_code == 200
    assert component["status"] == "UP"
    assert component["details"]["sources"]["config"]["status"] == "STALE"


async def test_writes_without_management_security_log_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.auto_configuration"):
        async with _client(_config(WRITABLE)):
            pass
    assert any(r.getMessage() == "feature_flags_writes_without_management_security" for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.auto_configuration"):
        async with _client(_config(WRITABLE, management={"security": {"enabled": True}})):
            pass
    assert not any(r.getMessage() == "feature_flags_writes_without_management_security" for r in caplog.records)


@configuration
class ExternalProvider:
    @bean
    def provider(self) -> InMemoryProvider:
        return InMemoryProvider({"x": InMemoryFlag("on", {"on": True})})


async def test_an_external_provider_is_reported_without_flags() -> None:
    async with _client(_config(), ExternalProvider) as (client, _):
        listing = (await client.get("/actuator/flags")).json()
    assert listing["provider"]["name"] == "In-Memory Provider"
    assert (listing["writable"], listing["sources"], listing["flags"]) == (False, [], [])


async def test_configprops_reports_bound_settings_with_literal_flag_placeholders() -> None:
    config = _config(
        {
            "flags": {
                "message": "Hello ${missing}",
                "special": {"state": "ENABLED", "variants": {"on": "${missing}"}, "defaultVariant": "on"},
            },
            "evaluators": {"note": {"in": ["${", {"var": "text"}]}},
            "management": {"writes": True},
        }
    )
    async with _client(config) as (client, _):
        response = await client.get("/actuator/configprops")
    assert response.status_code == 200
    beans = response.json()["contexts"]["application"]["beans"]
    properties = beans["FeatureFlagsProperties"]["properties"]
    assert properties["management"]["writes"] is True
    assert "flags" not in properties and "evaluators" not in properties
