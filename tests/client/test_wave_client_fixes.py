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
"""Regression tests for client audit fixes (#12, #13, #14, #18)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pyfly.client.declarative import get, http_client, post, service_client
from pyfly.client.exceptions import ServiceNotFoundException, ServiceValidationException
from pyfly.client.post_processor import HttpClientBeanPostProcessor


class _Resp:
    def __init__(self, body: bytes = b"{}", status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return json.loads(self._body)

    @property
    def text(self) -> str:
        return self._body.decode()


class _FakeClient:
    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[dict] = []
        self.stopped = False
        self._status = status_code

    async def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Resp(b'{"ok": true}', status_code=self._status)

    async def start(self) -> None: ...

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_error_status_raises_typed_exception() -> None:
    @http_client(base_url="http://x")
    class C:
        @get("/missing")
        async def fetch(self) -> dict: ...

    fake = _FakeClient(status_code=404)
    processor = HttpClientBeanPostProcessor(http_client_factory=lambda _b: fake)
    bean = C()
    processor.after_init(bean, "c")

    with pytest.raises(ServiceNotFoundException):
        await bean.fetch()


@pytest.mark.asyncio
async def test_non_retryable_error_not_retried() -> None:
    @service_client(base_url="http://x", retry=3, circuit_breaker=False)
    class C:
        @get("/bad")
        async def fetch(self) -> dict: ...

    fake = _FakeClient(status_code=400)  # ServiceValidationException is not retryable
    processor = HttpClientBeanPostProcessor(http_client_factory=lambda _b: fake)
    bean = C()
    processor.after_init(bean, "c")

    with pytest.raises(ServiceValidationException):
        await bean.fetch()
    assert len(fake.calls) == 1  # not retried (audit #13)


@pytest.mark.asyncio
async def test_stop_closes_clients() -> None:
    @http_client(base_url="http://x")
    class C:
        @get("/ok")
        async def fetch(self) -> dict: ...

    fake = _FakeClient()
    processor = HttpClientBeanPostProcessor(http_client_factory=lambda _b: fake)
    processor.after_init(C(), "c")

    await processor.stop()
    assert fake.stopped is True  # audit #14


@pytest.mark.asyncio
async def test_headers_param_sent_as_headers() -> None:
    @http_client(base_url="http://x")
    class C:
        @post("/items")
        async def create(self, body: dict, headers: dict) -> dict: ...

    fake = _FakeClient()
    processor = HttpClientBeanPostProcessor(http_client_factory=lambda _b: fake)
    bean = C()
    processor.after_init(bean, "c")

    await bean.create({"a": 1}, {"X-Trace": "abc"})
    call = fake.calls[0]
    assert call.get("headers") == {"X-Trace": "abc"}  # audit #18
    assert "headers" not in call.get("params", {})
