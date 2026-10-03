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
"""The sync server's auth, exact body hash, conditional GET, and web adapter wiring."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.server import DEFAULT_PATH, FlagSyncServer, build_feature_flag_server_routes
from pyfly.feature_flags.sources.http import HttpFlagSource
from pyfly.web.adapters.fastapi.app import create_app as create_fastapi_app
from pyfly.web.adapters.starlette.app import create_app as create_starlette_app
from tests.feature_flags.support import StaticSource, bool_flag


async def _registry() -> FlagRegistry:
    registry = FlagRegistry(
        [StaticSource("config", {"b": True, "a": "v1"}, evaluators={"beta": {"in": ["beta", {"var": "roles"}]}})],
        FireflyFlagProvider(),
    )
    await registry.start()
    return registry


def _client(server: FlagSyncServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=server.routes())), base_url="http://test"
    )


async def test_the_document_is_served_with_its_etag_to_a_bearer() -> None:
    registry = await _registry()
    server = FlagSyncServer(registry, token="s3cret")
    async with _client(server) as client:
        response = await client.get(DEFAULT_PATH, headers={"Authorization": "Bearer s3cret"})
        write_response = await client.post(DEFAULT_PATH, headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert write_response.status_code == 405
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"] == f'"{hashlib.sha256(response.content).hexdigest()}"'
    assert (
        response.content
        == json.dumps(registry.document(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert list(response.json()["flags"]) == ["a", "b"]
    await registry.stop()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "s3cret", "Basic czNjcmV0", "Bearer S3CRET"])
async def test_a_missing_or_wrong_token_is_401(authorization: str | None) -> None:
    registry = await _registry()
    headers = {"Authorization": authorization} if authorization is not None else {}
    async with _client(FlagSyncServer(registry, token="s3cret")) as client:
        response = await client.get(DEFAULT_PATH, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"] == "unauthorized"
    assert "etag" not in response.headers
    await registry.stop()


async def test_if_none_match_with_the_current_etag_is_304() -> None:
    registry = await _registry()
    server = FlagSyncServer(registry, token="t")
    _, etag = server.body_and_etag()
    async with _client(server) as client:
        auth = {"Authorization": "Bearer t"}
        for match in (etag, f'"x", {etag}', f"W/{etag}"):
            response = await client.get(DEFAULT_PATH, headers={**auth, "If-None-Match": match})
            assert response.status_code == 304 and response.content == b""
            assert response.headers["etag"] == etag and response.headers["cache-control"] == "no-cache"
        assert (await client.get(DEFAULT_PATH, headers={**auth, "If-None-Match": '"stale"'})).status_code == 200
    await registry.stop()


async def test_test_overrides_are_never_served() -> None:
    registry = await _registry()
    registry.set_test_overrides({"b": False, "only-in-tests": True})
    body, _ = FlagSyncServer(registry, allow_anonymous=True).body_and_etag()
    assert set(json.loads(body)["flags"]) == {"a", "b"}
    assert json.loads(body)["flags"]["b"] == bool_flag("on")
    await registry.stop()


async def test_anonymous_serving_needs_an_explicit_opt_in() -> None:
    registry = await _registry()
    with pytest.raises(ValueError, match="token"):
        FlagSyncServer(registry)
    async with _client(FlagSyncServer(registry, allow_anonymous=True)) as client:
        assert (await client.get(DEFAULT_PATH)).status_code == 200
    await registry.stop()


@pytest.mark.parametrize("path", ["", "/", "//", "/./", "/foo/../", "/%2e/"])
async def test_a_root_sync_path_is_refused(path: str) -> None:
    registry = await _registry()
    with pytest.raises(ValueError, match="path"):
        FlagSyncServer(registry, path=path, token="t")
    await registry.stop()


async def test_the_server_feeds_an_http_source_in_another_registry() -> None:
    upstream = await _registry()
    app = Starlette(routes=FlagSyncServer(upstream, token="t").routes())
    source = HttpFlagSource(f"http://control-plane{DEFAULT_PATH}", token="t", transport=httpx.ASGITransport(app=app))
    downstream = FlagRegistry([source], FireflyFlagProvider())
    await downstream.start()
    assert downstream.document() == upstream.document()
    assert await downstream.refresh("http") == []
    assert downstream.sources()[0].status == "UP"
    await downstream.stop()
    await upstream.stop()


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
@pytest.mark.parametrize("started", [False, True])
async def test_app_mounts_the_route_of_a_server_bean(adapter: str, started: bool) -> None:
    registry = await _registry()
    context = ApplicationContext(Config({}))
    server = FlagSyncServer(registry, path="/flags.json", token="t")
    assert build_feature_flag_server_routes(None) == []
    create_app = create_starlette_app if adapter == "starlette" else create_fastapi_app
    if started:
        context.container.register_instance(FlagSyncServer, server)
        await context.start()
        app = create_app(context=context, actuator_enabled=False, docs_enabled=False)
        lifespan = None
    else:

        @asynccontextmanager
        async def boot(_: Any) -> AsyncIterator[None]:
            await context.start()
            context.container.register_instance(FlagSyncServer, server)
            try:
                yield
            finally:
                await context.stop()

        app = create_app(context=context, lifespan=boot, actuator_enabled=False, docs_enabled=False)
        lifespan = app.router.lifespan_context(app)
    if lifespan is None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/flags.json", headers={"Authorization": "Bearer t"})).status_code == 200
        await context.stop()
    else:
        async with (
            lifespan,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert (await client.get("/flags.json", headers={"Authorization": "Bearer t"})).status_code == 200
    await registry.stop()
