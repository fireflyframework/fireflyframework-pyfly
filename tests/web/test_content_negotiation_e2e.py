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
"""End-to-end content negotiation (v26.06.28): a real controller served as JSON or
XML by Accept, and request bodies parsed from JSON or XML by Content-Type."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.mappings import get_mapping, post_mapping
from pyfly.web.params import Body


class Widget(BaseModel):
    name: str
    qty: int


@rest_controller
class WidgetController:
    @get_mapping("/widget")
    async def get_widget(self) -> Widget:
        return Widget(name="gadget", qty=3)

    @post_mapping("/widget")
    async def echo_widget(self, widget: Body[Widget]) -> Widget:
        return widget


async def _client() -> TestClient:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(WidgetController)
    await ctx.start()
    return TestClient(create_app(context=ctx))


@pytest.mark.asyncio
async def test_response_negotiates_json_and_xml() -> None:
    client = await _client()
    rj = client.get("/widget", headers={"accept": "application/json"})
    assert rj.status_code == 200
    assert rj.headers["content-type"].startswith("application/json")
    assert rj.json() == {"name": "gadget", "qty": 3}

    rx = client.get("/widget", headers={"accept": "application/xml"})
    assert rx.status_code == 200
    assert rx.headers["content-type"].startswith("application/xml")
    assert "<name>gadget</name>" in rx.text
    assert "<qty>3</qty>" in rx.text


@pytest.mark.asyncio
async def test_request_body_parsed_from_json_and_xml() -> None:
    client = await _client()
    rj = client.post("/widget", json={"name": "x", "qty": 5})
    assert rj.status_code == 200
    assert rj.json() == {"name": "x", "qty": 5}

    # XML request body (the newly added capability) — Content-Type drives the reader.
    xml = "<widget><name>y</name><qty>7</qty></widget>"
    rx = client.post("/widget", content=xml, headers={"content-type": "application/xml"})
    assert rx.status_code == 200
    assert rx.json() == {"name": "y", "qty": 7}
