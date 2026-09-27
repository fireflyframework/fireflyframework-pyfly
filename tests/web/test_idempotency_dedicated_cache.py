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
"""Idempotency records live in a cache of their own (C034).

The auto-configured idempotency filter used to store its records in the application's ``CacheAdapter``
bean, so clearing that cache (the admin's "evict all", ``@cache_evict(all_entries=True)``, the CQRS
query-cache reset) dropped them, and a retried ``POST`` with the same ``Idempotency-Key`` ran again.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters.idempotency_filter import IDEMPOTENCY_KEY_HEADER
from pyfly.web.idempotency_auto_configuration import IdempotencyFilterAutoConfiguration


def test_clearing_the_cache_keeps_idempotency_records() -> None:
    payments: list[int] = []

    async def pay(request: Request) -> JSONResponse:
        payments.append(len(payments) + 1)
        return JSONResponse({"payment": len(payments)}, status_code=201)

    cache = InMemoryCache()
    config = Config({"pyfly": {"web": {"idempotency": {"enabled": True}}}})
    web_filter = IdempotencyFilterAutoConfiguration().idempotency_web_filter(config, cache=cache)
    app = Starlette(
        routes=[Route("/payments", pay, methods=["POST"])],
        middleware=[Middleware(WebFilterChainMiddleware, filters=[web_filter])],
    )
    client = TestClient(app)

    first = client.post("/payments", headers={IDEMPOTENCY_KEY_HEADER: "key-1"})
    assert first.status_code == 201
    assert cache.get_keys() == []  # not in the application cache

    import asyncio

    asyncio.run(cache.clear())
    retried = client.post("/payments", headers={IDEMPOTENCY_KEY_HEADER: "key-1"})
    assert retried.status_code == 201
    assert retried.json() == {"payment": 1}
    assert payments == [1]
