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
"""Tests for ParameterResolver — auto-binding from Starlette Request."""

import json

import pytest
from pydantic import BaseModel

from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.params import Body, Cookie, Header, PathVar, QueryParam


class CreateItem(BaseModel):
    name: str
    price: float


class TestParameterResolverInspection:
    def test_detects_path_var(self):
        async def handler(self, item_id: PathVar[str]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        assert resolver.params[0].name == "item_id"
        assert resolver.params[0].binding_type is PathVar

    def test_detects_query_param(self):
        async def handler(self, page: QueryParam[int] = 1):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        assert resolver.params[0].name == "page"
        assert resolver.params[0].binding_type is QueryParam
        assert resolver.params[0].default == 1

    def test_detects_body(self):
        async def handler(self, body: Body[CreateItem]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        assert resolver.params[0].binding_type is Body
        assert resolver.params[0].inner_type is CreateItem

    def test_detects_header(self):
        async def handler(self, x_api_key: Header[str]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        assert resolver.params[0].binding_type is Header

    def test_detects_cookie(self):
        async def handler(self, session: Cookie[str]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        assert resolver.params[0].binding_type is Cookie

    def test_skips_self(self):
        async def handler(self, item_id: PathVar[str]):
            pass

        resolver = ParameterResolver(handler)
        assert all(p.name != "self" for p in resolver.params)

    def test_multiple_params(self):
        async def handler(self, item_id: PathVar[str], page: QueryParam[int] = 1):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 2


class TestParameterResolverResolve:
    @pytest.mark.asyncio
    async def test_resolve_path_var(self):
        async def handler(self, order_id: PathVar[str]):
            pass

        resolver = ParameterResolver(handler)

        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/orders/abc-123",
            "path_params": {"order_id": "abc-123"},
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"order_id": "abc-123"}

    @pytest.mark.asyncio
    async def test_resolve_path_var_int(self):
        async def handler(self, item_id: PathVar[int]):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/items/42",
            "path_params": {"item_id": "42"},
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"item_id": 42}

    @pytest.mark.asyncio
    async def test_resolve_query_param(self):
        async def handler(self, page: QueryParam[int] = 1):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/items",
            "path_params": {},
            "query_string": b"page=5",
            "headers": [],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"page": 5}

    @pytest.mark.asyncio
    async def test_resolve_query_param_default(self):
        async def handler(self, page: QueryParam[int] = 1):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/items",
            "path_params": {},
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"page": 1}

    @pytest.mark.asyncio
    async def test_resolve_body(self):
        async def handler(self, body: Body[CreateItem]):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        body_bytes = json.dumps({"name": "Widget", "price": 9.99}).encode()

        async def receive():
            return {"type": "http.request", "body": body_bytes}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/items",
            "path_params": {},
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope, receive)
        kwargs = await resolver.resolve(request)
        assert isinstance(kwargs["body"], CreateItem)
        assert kwargs["body"].name == "Widget"
        assert kwargs["body"].price == 9.99

    @pytest.mark.asyncio
    async def test_resolve_header(self):
        async def handler(self, x_api_key: Header[str]):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/items",
            "path_params": {},
            "query_string": b"",
            "headers": [(b"x-api-key", b"secret-123")],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"x_api_key": "secret-123"}

    @pytest.mark.asyncio
    async def test_resolve_cookie(self):
        async def handler(self, session_id: Cookie[str]):
            pass

        resolver = ParameterResolver(handler)
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/items",
            "path_params": {},
            "query_string": b"",
            "headers": [(b"cookie", b"session_id=abc-session")],
        }
        request = Request(scope)
        kwargs = await resolver.resolve(request)
        assert kwargs == {"session_id": "abc-session"}
