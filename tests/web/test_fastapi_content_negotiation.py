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
"""v26.06.33: the FastAPI adapter now wires the central JSON layer, the
HttpMessageConverter chain (content negotiation), and RFC 7807 — previously it
bypassed all of them. Mirrors the Starlette content-negotiation e2e test."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from pydantic import BaseModel  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from pyfly.container.stereotypes import rest_controller  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.kernel.exceptions import ResourceNotFoundException  # noqa: E402
from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter  # noqa: E402
from pyfly.web.mappings import get_mapping, post_mapping  # noqa: E402
from pyfly.web.params import Body  # noqa: E402


class Gadget(BaseModel):
    name: str
    qty: int


@rest_controller
class GadgetController:
    @get_mapping("/gadget")
    async def get_gadget(self) -> Gadget:
        return Gadget(name="widget", qty=2)

    @post_mapping("/gadget")
    async def echo_gadget(self, gadget: Body[Gadget]) -> Gadget:
        return gadget

    @get_mapping("/missing")
    async def missing(self) -> Gadget:
        raise ResourceNotFoundException("gone", code="GADGET_NOT_FOUND")


async def _client(config: dict | None = None) -> TestClient:
    ctx = ApplicationContext(Config(config or {}))
    ctx.register_bean(GadgetController)
    await ctx.start()
    app = FastAPIWebAdapter().create_app(context=ctx, docs_enabled=False)
    # raise_server_exceptions=False so the registered exception handler produces the
    # response (as in a real server) instead of TestClient re-raising it.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.asyncio
async def test_fastapi_response_negotiates_json_and_xml() -> None:
    client = await _client()
    rj = client.get("/gadget", headers={"accept": "application/json"})
    assert rj.headers["content-type"].startswith("application/json")
    assert rj.json() == {"name": "widget", "qty": 2}

    rx = client.get("/gadget", headers={"accept": "application/xml"})
    assert rx.headers["content-type"].startswith("application/xml")
    assert "<name>widget</name>" in rx.text


@pytest.mark.asyncio
async def test_fastapi_parses_xml_request_body() -> None:
    client = await _client()
    rx = client.post(
        "/gadget",
        content="<gadget><name>y</name><qty>7</qty></gadget>",
        headers={"content-type": "application/xml"},
    )
    assert rx.status_code == 200
    assert rx.json() == {"name": "y", "qty": 7}


@pytest.mark.asyncio
async def test_fastapi_problem_details_when_enabled() -> None:
    client = await _client({"pyfly": {"web": {"problem-details": {"enabled": True}}}})
    resp = client.get("/missing")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["title"] == "Not Found"
    assert body["code"] == "GADGET_NOT_FOUND"
