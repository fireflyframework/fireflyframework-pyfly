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
"""End-to-end declarative HTTP client integration test."""

from __future__ import annotations

import json

import pytest

from pyfly.client.declarative import get, http_client, post, service_client
from pyfly.client.post_processor import HttpClientBeanPostProcessor
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


class FakeHttpClient:
    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url
        self.calls: list[dict] = []
        self.responses: dict[str, bytes] = {}

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        body = self.responses.get(f"{method}:{url}", b"{}")
        return FakeResponse(body)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict:
        return json.loads(self._body)


class TestDeclarativeClientIntegration:
    @pytest.mark.asyncio
    async def test_full_lifecycle_with_context(self) -> None:
        fake = FakeHttpClient()
        fake.responses["GET:/users/1"] = json.dumps({"id": 1, "name": "Alice"}).encode()
        fake.responses["POST:/users"] = json.dumps({"id": 2, "name": "Bob"}).encode()

        @http_client(base_url="http://api.example.com")
        class UserClient:
            @get("/users/{user_id}")
            async def get_user(self, user_id: int) -> dict: ...

            @post("/users")
            async def create_user(self, body: dict) -> dict: ...

        ctx = ApplicationContext(Config())
        ctx.register_bean(UserClient)
        ctx.register_post_processor(HttpClientBeanPostProcessor(http_client_factory=lambda base_url: fake))
        await ctx.start()

        client = ctx.get_bean(UserClient)
        user = await client.get_user(1)
        assert user == {"id": 1, "name": "Alice"}

        created = await client.create_user(body={"name": "Bob"})
        assert created == {"id": 2, "name": "Bob"}

        assert len(fake.calls) == 2
        assert fake.calls[0]["method"] == "GET"
        assert fake.calls[1]["method"] == "POST"

    @pytest.mark.asyncio
    async def test_service_client_lifecycle_with_resilience(self) -> None:
        fake = FakeHttpClient()
        fake.responses["GET:/items/1"] = json.dumps({"id": 1}).encode()

        @service_client(base_url="http://api.example.com", retry=2, circuit_breaker=True)
        class ItemClient:
            @get("/items/{item_id}")
            async def get_item(self, item_id: int) -> dict: ...

        ctx = ApplicationContext(Config())
        ctx.register_bean(ItemClient)
        ctx.register_post_processor(HttpClientBeanPostProcessor(http_client_factory=lambda base_url: fake))
        await ctx.start()

        client = ctx.get_bean(ItemClient)
        result = await client.get_item(1)
        assert result == {"id": 1}
        assert len(fake.calls) == 1
