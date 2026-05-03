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
"""Tests for HttpClientBeanPostProcessor."""

from __future__ import annotations

import json

import pytest

from pyfly.client.declarative import get, http_client, post, service_client
from pyfly.client.post_processor import HttpClientBeanPostProcessor
from pyfly.kernel.exceptions import CircuitBreakerException


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response_body: bytes = b"{}"
        self.response_status: int = 200

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(self.response_body, self.response_status)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return json.loads(self._body)

    @property
    def text(self) -> str:
        return self._body.decode()


class TestHttpClientBeanPostProcessor:
    @pytest.mark.asyncio
    async def test_wires_get_method(self) -> None:
        @http_client(base_url="http://api.example.com")
        class ItemClient:
            @get("/items/{item_id}")
            async def get_item(self, item_id: str) -> dict: ...

        fake = FakeHttpClient()
        fake.response_body = json.dumps({"id": "abc", "name": "Widget"}).encode()
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = ItemClient()
        processor.before_init(bean, "itemClient")
        processor.after_init(bean, "itemClient")
        result = await bean.get_item("abc")
        assert result == {"id": "abc", "name": "Widget"}
        assert fake.calls[0]["method"] == "GET"
        assert fake.calls[0]["url"] == "/items/abc"

    @pytest.mark.asyncio
    async def test_wires_post_method_with_body(self) -> None:
        @http_client(base_url="http://api.example.com")
        class ItemClient:
            @post("/items")
            async def create_item(self, body: dict) -> dict: ...

        fake = FakeHttpClient()
        fake.response_body = json.dumps({"id": "new"}).encode()
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = ItemClient()
        processor.before_init(bean, "itemClient")
        processor.after_init(bean, "itemClient")
        result = await bean.create_item(body={"name": "Widget"})
        assert result == {"id": "new"}
        assert fake.calls[0]["method"] == "POST"

    def test_skips_non_http_client_beans(self) -> None:
        class RegularBean:
            pass

        processor = HttpClientBeanPostProcessor()
        bean = RegularBean()
        result = processor.after_init(bean, "regularBean")
        assert result is bean

    @pytest.mark.asyncio
    async def test_path_variable_substitution(self) -> None:
        @http_client(base_url="http://example.com")
        class UserClient:
            @get("/users/{user_id}/posts/{post_id}")
            async def get_post(self, user_id: int, post_id: int) -> dict: ...

        fake = FakeHttpClient()
        fake.response_body = b'{"title": "Hello"}'
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = UserClient()
        processor.before_init(bean, "userClient")
        processor.after_init(bean, "userClient")
        await bean.get_post(42, 7)
        assert fake.calls[0]["url"] == "/users/42/posts/7"


class FailingHttpClient:
    """HTTP client that fails a configurable number of times, then succeeds."""

    def __init__(self, fail_count: int = 0) -> None:
        self.calls: list[dict] = []
        self._fail_count = fail_count
        self._call_num = 0

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self._call_num += 1
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._call_num <= self._fail_count:
            raise ConnectionError(f"Simulated failure #{self._call_num}")
        return FakeResponse(b'{"ok": true}')

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class TestPostProcessorResilience:
    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        """Retry wraps failures and eventually succeeds."""

        @service_client(base_url="http://example.com", retry=3, circuit_breaker=False)
        class MyClient:
            @get("/data")
            async def get_data(self) -> dict: ...

        fake = FailingHttpClient(fail_count=2)  # fail twice, succeed third
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = MyClient()
        processor.after_init(bean, "myClient")
        result = await bean.get_data()
        assert result == {"ok": True}
        assert len(fake.calls) == 3

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens(self) -> None:
        """Circuit breaker opens after failure threshold is reached."""

        @service_client(
            base_url="http://example.com",
            retry=False,
            circuit_breaker=True,
            circuit_breaker_failure_threshold=2,
        )
        class MyClient:
            @get("/data")
            async def get_data(self) -> dict: ...

        fake = FailingHttpClient(fail_count=100)
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = MyClient()
        processor.after_init(bean, "myClient")

        # First two calls fail with ConnectionError
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await bean.get_data()

        # Third call should be rejected by the circuit breaker
        with pytest.raises(CircuitBreakerException):
            await bean.get_data()

    @pytest.mark.asyncio
    async def test_http_client_has_no_resilience(self) -> None:
        """@http_client beans have no retry or circuit breaker."""

        @http_client(base_url="http://example.com")
        class MyClient:
            @get("/data")
            async def get_data(self) -> dict: ...

        fake = FailingHttpClient(fail_count=1)
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
        )
        bean = MyClient()
        processor.after_init(bean, "myClient")

        # Should fail immediately — no retry
        with pytest.raises(ConnectionError):
            await bean.get_data()
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_config_defaults_override(self) -> None:
        """Config-provided defaults are used when decorator doesn't specify."""

        @service_client(base_url="http://example.com")
        class MyClient:
            @get("/data")
            async def get_data(self) -> dict: ...

        fake = FailingHttpClient(fail_count=4)  # fail 4 times
        processor = HttpClientBeanPostProcessor(
            http_client_factory=lambda base_url: fake,
            default_retry={"max-attempts": 5, "base-delay": 0.0},
            default_circuit_breaker={"failure-threshold": 10, "recovery-timeout": 30},
        )
        bean = MyClient()
        processor.after_init(bean, "myClient")

        # With max-attempts=5, should survive 4 failures
        result = await bean.get_data()
        assert result == {"ok": True}
        assert len(fake.calls) == 5
