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
"""Shared conditional HTTP behavior through real sync and polling adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.server import FlagSyncServer
from pyfly.feature_flags.sources.config import ConfigFlagSource
from pyfly.feature_flags.sources.http import HttpFlagSource

CASES: list[dict[str, Any]] = json.loads((Path(__file__).parent / "conformance" / "http-vectors.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "server"], ids=lambda c: c["name"])
async def test_shared_sync_conditions(case: dict[str, Any]) -> None:
    registry = FlagRegistry([ConfigFlagSource({"a": True})], FireflyFlagProvider())
    await registry.start()
    server = FlagSyncServer(registry, token="proof")
    body, etag = server.body_and_etag()
    headers: dict[str, str] = {}
    if case["authorization"] != "missing":
        headers["Authorization"] = "Bearer " + ("proof" if case["authorization"] == "valid" else "wrong")
    if case["condition"] is not None:
        headers["If-None-Match"] = case["condition"].replace("{etag}", etag)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=Starlette(routes=server.routes())), base_url="http://test"
        ) as client:
            response = await client.get(server.path, headers=headers)
        assert response.status_code == case["expect"]["status"]
        if response.status_code == 200:
            assert response.content == body
        elif response.status_code == 304:
            assert response.content == b"" and response.headers["etag"] == etag
        else:
            assert "etag" not in response.headers
    finally:
        await registry.stop()


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "client"], ids=lambda c: c["name"])
async def test_shared_poll_conditions(case: dict[str, Any]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if case["firstEtag"] is None or len(requests) > 1:
            return httpx.Response(304)
        headers = {"ETag": case["firstEtag"]} if case["firstEtag"] else {}
        return httpx.Response(200, content=case["body"].encode(), headers=headers)

    source = HttpFlagSource("http://test/flags", token="proof", transport=httpx.MockTransport(respond))
    try:
        if case["expect"]["error"]:
            with pytest.raises(httpx.HTTPStatusError):
                await source.load()
        else:
            snapshot = await source.load()
            expected = case["firstEtag"] or f'"{hashlib.sha256(case["body"].encode()).hexdigest()}"'
            assert snapshot is not None and snapshot.revision == expected
            assert snapshot.document.flags["a"]["defaultVariant"] == "on"
            assert await source.load() is None
            assert requests[1].headers["if-none-match"] == expected
        assert "if-none-match" not in requests[0].headers
        assert requests[0].headers["authorization"] == "Bearer proof"
    finally:
        await source.close()


async def test_an_unsolicited_304_cannot_borrow_a_later_requests_revision() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if not started.is_set():
            started.set()
            await release.wait()
            return httpx.Response(304)
        return httpx.Response(200, json={"flags": {}}, headers={"ETag": '"accepted"'})

    source = HttpFlagSource("http://test/flags", transport=httpx.MockTransport(respond))
    initial = asyncio.create_task(source.load())
    try:
        await asyncio.wait_for(started.wait(), 2)
        accepted = await source.load()
        assert accepted is not None
        release.set()
        with pytest.raises(httpx.HTTPStatusError):
            await asyncio.wait_for(initial, 2)
    finally:
        release.set()
        if not initial.done():
            initial.cancel()
        await asyncio.gather(initial, return_exceptions=True)
        await source.close()
