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
"""The http source: conditional GET, authentication, validation and last-good recovery (spec 4.7)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from pyfly.feature_flags.definitions import FlagDefinitionError
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.sources import FlagSource
from pyfly.feature_flags.sources.http import HttpFlagSource
from tests.feature_flags.support import bool_flag

URL = "http://control-plane/feature-flags/flagd.json"


class Server:
    """Answer scripted requests and retain the requests to check the HTTP boundary."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _document(**flags: Any) -> httpx.Response:
    return httpx.Response(200, json={"flags": flags}, headers={"ETag": f'"{len(flags)}-{sorted(flags)}"'})


async def test_conditional_get_sends_the_token_and_accepted_etag_and_304_is_unchanged() -> None:
    server = Server(_document(a=bool_flag()), httpx.Response(304))
    source = HttpFlagSource(URL, token="s3cret", refresh_interval=10, timeout=1, transport=httpx.MockTransport(server))
    assert isinstance(source, FlagSource)
    assert (source.name, source.fail_fast, source.refresh_interval) == ("http", False, 10)
    first = await source.load()
    assert first is not None and first.document.flags == {"a": bool_flag()}
    assert first.revision == "\"1-['a']\""
    assert await source.load() is None
    assert server.requests[0].headers["authorization"] == "Bearer s3cret"
    assert server.requests[0].headers["accept"] == "application/json"
    assert "if-none-match" not in server.requests[0].headers
    assert server.requests[1].headers["if-none-match"] == first.revision
    await source.close()


async def test_without_a_token_no_authorization_header_is_sent() -> None:
    server = Server(_document())
    source = HttpFlagSource(URL, transport=httpx.MockTransport(server))
    await source.load()
    assert "authorization" not in server.requests[0].headers
    await source.close()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.Response(500, text="boom"), httpx.HTTPStatusError),
        (httpx.Response(401, json={"error": "unauthorized"}), httpx.HTTPStatusError),
        (httpx.Response(302, headers={"Location": "/other"}), httpx.HTTPStatusError),
        (httpx.ReadTimeout("slow"), httpx.ReadTimeout),
        (httpx.ConnectError("refused"), httpx.ConnectError),
        (httpx.Response(200, text="<html>not json</html>"), ValueError),
    ],
)
async def test_a_failed_poll_raises_for_registry_last_good_handling(
    failure: httpx.Response | Exception, expected: type[Exception]
) -> None:
    source = HttpFlagSource(URL, transport=httpx.MockTransport(Server(failure)))
    with pytest.raises(expected):
        await source.load()
    await source.close()


async def test_rejected_full_document_does_not_advance_etag() -> None:
    invalid = httpx.Response(200, json={"flags": {"bad key": bool_flag()}}, headers={"ETag": '"v2"'})
    server = Server(_document(a=bool_flag()), invalid, _document(a=bool_flag("off"), b=bool_flag()))
    source = HttpFlagSource(URL, transport=httpx.MockTransport(server))
    first = await source.load()
    assert first is not None
    with pytest.raises(FlagDefinitionError, match="invalid flag key"):
        await source.load()
    third = await source.load()
    assert third is not None and third.document.flags["a"] == bool_flag("off")
    assert server.requests[2].headers["if-none-match"] == first.revision
    await source.close()


async def test_document_load_preserves_canonical_flags_evaluators_and_metadata() -> None:
    server = Server(
        httpx.Response(
            200,
            json={
                "flags": {"a": {**bool_flag(), "targeting": [], "metadata": []}},
                "$evaluators": {"beta": {"in": ["beta", {"var": "roles"}]}},
                "metadata": {"owner": "platform"},
            },
            headers={"ETag": '"full"'},
        )
    )
    source = HttpFlagSource(URL, transport=httpx.MockTransport(server))
    snapshot = await source.load()
    assert snapshot is not None
    assert snapshot.document.flags == {"a": bool_flag(targeting={}, metadata={})}
    assert snapshot.document.evaluators == {"beta": {"in": ["beta", {"var": "roles"}]}}
    assert snapshot.document.metadata == {"owner": "platform"}
    await source.close()


async def test_registry_keeps_last_good_document_and_reports_stale_then_recovers_on_304() -> None:
    server = Server(_document(a=bool_flag()), httpx.ReadTimeout("slow"), httpx.Response(304))
    registry = FlagRegistry([HttpFlagSource(URL, transport=httpx.MockTransport(server))], FireflyFlagProvider())
    await registry.start()
    await registry.refresh("http")
    [status] = registry.sources()
    assert status.status == "STALE" and status.error == "ReadTimeout: slow"
    assert registry.provider.definition("a") == bool_flag()
    await registry.refresh("http")
    assert registry.sources()[0].status == "UP"
    assert server.requests[2].headers["if-none-match"] == server.requests[1].headers["if-none-match"]
    await registry.stop()


async def test_provider_refusal_retries_the_same_http_revision_without_a_conditional_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"flags": {"a": bool_flag("off")}}, headers={"ETag": '"v1"'})
        if request.headers.get("if-none-match") == '"v2"':
            return httpx.Response(304)
        return httpx.Response(200, json={"flags": {"a": bool_flag("on")}}, headers={"ETag": '"v2"'})

    registry = FlagRegistry([HttpFlagSource(URL, transport=httpx.MockTransport(server))], FireflyFlagProvider())
    await registry.start()
    update = registry.provider.update
    refused = False

    def refuse_once(*args: Any, **kwargs: Any) -> None:
        nonlocal refused
        if not refused:
            refused = True
            raise RuntimeError("provider refused")
        update(*args, **kwargs)

    monkeypatch.setattr(registry.provider, "update", refuse_once)
    assert await registry.refresh("http") == []
    assert registry.sources()[0].status == "STALE"
    assert registry.provider.definition("a") == bool_flag("off")
    assert await registry.refresh("http") == ["a"]
    assert "if-none-match" not in requests[2].headers
    assert registry.sources()[0].status == "UP"
    assert registry.provider.definition("a") == bool_flag("on")
    await registry.stop()


async def test_older_http_load_cannot_restore_a_newer_refused_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    older_started = asyncio.Event()
    release_older = asyncio.Event()

    async def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"flags": {"a": bool_flag("off")}}, headers={"ETag": '"v1"'})
        if len(requests) == 2:
            older_started.set()
            await release_older.wait()
        if request.headers.get("if-none-match") == '"v2"':
            return httpx.Response(304)
        return httpx.Response(200, json={"flags": {"a": bool_flag("on")}}, headers={"ETag": '"v2"'})

    registry = FlagRegistry([HttpFlagSource(URL, transport=httpx.MockTransport(server))], FireflyFlagProvider())
    await registry.start()
    older = asyncio.create_task(registry.refresh("http"))
    try:
        await older_started.wait()

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("provider refused")

        update = registry.provider.update
        monkeypatch.setattr(registry.provider, "update", refuse)
        assert await registry.refresh("http") == []
        assert registry.sources()[0].status == "STALE"
        monkeypatch.setattr(registry.provider, "update", update)
        release_older.set()
        assert await older == []

        assert await registry.refresh("http") == ["a"]
        assert [request.headers.get("if-none-match") for request in requests] == [None, '"v1"', '"v1"', None]
        assert registry.provider.definition("a") == bool_flag("on")
        assert registry.sources()[0].status == "UP"
    finally:
        release_older.set()
        await older
        await registry.stop()


def test_a_url_is_required() -> None:
    with pytest.raises(ValueError, match="sources.http.url"):
        HttpFlagSource("")


async def test_timeout_is_passed_to_client() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, content=json.dumps({"flags": {}}).encode())

    source = HttpFlagSource(URL, timeout=1.5, transport=httpx.MockTransport(handler))
    await source.load()
    assert seen == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}
    await source.close()
