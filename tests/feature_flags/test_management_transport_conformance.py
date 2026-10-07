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
"""Shared management transport cases through the real ASGI actuator routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app

VECTORS = json.loads((Path(__file__).parent / "conformance" / "management-transport-vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
async def test_management_transport_vector(case: dict[str, Any]) -> None:
    options = case.get("config", {})
    feature_flags = {
        "enabled": True,
        "flags": VECTORS["flags"],
        "sources": {"store": {"enabled": options.get("store", True), "driver": "memory"}},
        "management": {"writes": options.get("writes", True)},
    }
    config = Config(
        {
            "pyfly": {
                "management": {"endpoints": {"web": {"exposure": {"include": "*"}}}},
                "feature-flags": feature_flags,
            }
        }
    )
    context = ApplicationContext(config)
    await context.start()
    app = create_app(context=context, actuator_enabled=True, docs_enabled=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request: dict[str, Any] = {"headers": case.get("headers", {})}
            if "rawBody" in case:
                request["content"] = case["rawBody"]
            elif "body" in case:
                request["json"] = case["body"]
            response = await client.request(case["method"], case["path"], **request)
            assert response.status_code == case["expect"]["status"]["pyfly"]
            body = response.json()
            if "error" in case["expect"]:
                assert body["error"] == case["expect"]["error"]
                assert body["message"]
            if "value" in case["expect"]:
                assert body["value"] == case["expect"]["value"]
            if "actor" in case["expect"]:
                detail = (await client.get("/actuator/flags/kill")).json()
                assert detail["history"][0]["actor"] == case["expect"]["actor"]
    finally:
        await context.stop()
