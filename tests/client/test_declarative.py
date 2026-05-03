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
"""Tests for declarative @http_client, @service_client, and HTTP method decorators."""

from __future__ import annotations

import pytest

from pyfly.client.declarative import delete, get, http_client, patch, post, put, service_client
from pyfly.container.types import Scope


class TestHttpClientDecorator:
    def test_marks_class_with_metadata(self) -> None:
        @http_client(base_url="http://example.com")
        class MyClient:
            pass

        assert MyClient.__pyfly_http_client__ is True
        assert MyClient.__pyfly_http_base_url__ == "http://example.com"

    def test_marks_class_as_injectable(self) -> None:
        @http_client(base_url="http://example.com")
        class MyClient:
            pass

        assert MyClient.__pyfly_injectable__ is True
        assert MyClient.__pyfly_stereotype__ == "component"
        assert MyClient.__pyfly_scope__ is Scope.SINGLETON


class TestServiceClientDecorator:
    def test_marks_class_with_metadata(self) -> None:
        @service_client(base_url="http://example.com")
        class MyClient:
            pass

        assert MyClient.__pyfly_http_client__ is True
        assert MyClient.__pyfly_http_base_url__ == "http://example.com"
        assert MyClient.__pyfly_service_client__ is True

    def test_resilience_defaults(self) -> None:
        @service_client(base_url="http://example.com")
        class MyClient:
            pass

        res = MyClient.__pyfly_resilience__
        assert res["retry"] is True
        assert res["circuit_breaker"] is True
        assert res["retry_base_delay"] is None
        assert res["circuit_breaker_failure_threshold"] is None
        assert res["circuit_breaker_recovery_timeout"] is None

    def test_custom_retry_attempts(self) -> None:
        @service_client(base_url="http://example.com", retry=5)
        class MyClient:
            pass

        res = MyClient.__pyfly_resilience__
        assert res["retry"] == 5

    def test_resilience_disabled(self) -> None:
        @service_client(base_url="http://example.com", retry=False, circuit_breaker=False)
        class MyClient:
            pass

        res = MyClient.__pyfly_resilience__
        assert res["retry"] is False
        assert res["circuit_breaker"] is False

    def test_custom_circuit_breaker_params(self) -> None:
        @service_client(
            base_url="http://example.com",
            circuit_breaker_failure_threshold=10,
            circuit_breaker_recovery_timeout=60.0,
        )
        class MyClient:
            pass

        res = MyClient.__pyfly_resilience__
        assert res["circuit_breaker_failure_threshold"] == 10
        assert res["circuit_breaker_recovery_timeout"] == 60.0

    def test_http_client_is_alias_with_no_resilience(self) -> None:
        @http_client(base_url="http://example.com")
        class MyClient:
            pass

        res = MyClient.__pyfly_resilience__
        assert res["retry"] is False
        assert res["circuit_breaker"] is False
        assert MyClient.__pyfly_service_client__ is True


class TestHttpMethodDecorators:
    def test_get_decorator(self) -> None:
        @get("/items/{item_id}")
        async def get_item(self, item_id: str) -> dict: ...

        assert get_item.__pyfly_http_method__ == "GET"
        assert get_item.__pyfly_http_path__ == "/items/{item_id}"

    def test_post_decorator(self) -> None:
        @post("/items")
        async def create_item(self, body: dict) -> dict: ...

        assert create_item.__pyfly_http_method__ == "POST"
        assert create_item.__pyfly_http_path__ == "/items"

    def test_put_decorator(self) -> None:
        @put("/items/{item_id}")
        async def update_item(self, item_id: str, body: dict) -> dict: ...

        assert update_item.__pyfly_http_method__ == "PUT"
        assert update_item.__pyfly_http_path__ == "/items/{item_id}"

    def test_delete_decorator(self) -> None:
        @delete("/items/{item_id}")
        async def delete_item(self, item_id: str) -> None: ...

        assert delete_item.__pyfly_http_method__ == "DELETE"
        assert delete_item.__pyfly_http_path__ == "/items/{item_id}"

    def test_patch_decorator(self) -> None:
        @patch("/items/{item_id}")
        async def patch_item(self, item_id: str, body: dict) -> dict: ...

        assert patch_item.__pyfly_http_method__ == "PATCH"
        assert patch_item.__pyfly_http_path__ == "/items/{item_id}"

    def test_multiple_methods_on_one_client(self) -> None:
        @http_client(base_url="http://api.example.com")
        class ItemClient:
            @get("/items/{item_id}")
            async def get_item(self, item_id: str) -> dict: ...

            @post("/items")
            async def create_item(self, body: dict) -> dict: ...

            @delete("/items/{item_id}")
            async def delete_item(self, item_id: str) -> None: ...

        get_method = ItemClient.get_item
        post_method = ItemClient.create_item
        delete_method = ItemClient.delete_item

        assert get_method.__pyfly_http_method__ == "GET"
        assert get_method.__pyfly_http_path__ == "/items/{item_id}"
        assert post_method.__pyfly_http_method__ == "POST"
        assert post_method.__pyfly_http_path__ == "/items"
        assert delete_method.__pyfly_http_method__ == "DELETE"
        assert delete_method.__pyfly_http_path__ == "/items/{item_id}"

    @pytest.mark.asyncio
    async def test_placeholder_raises_not_implemented(self) -> None:
        @http_client(base_url="http://example.com")
        class MyClient:
            @get("/items")
            async def list_items(self) -> list: ...

        client = MyClient()
        with pytest.raises(NotImplementedError, match="has not been wired"):
            await client.list_items()
