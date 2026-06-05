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
"""Regression tests for web fixes.

#202 — ExceptionConverterService wired into the global error path.
#204 — CORS auto-configured from ``pyfly.web.cors.*``.
#206 — missing required QueryParam/Header/Cookie returns 400, not None.
"""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.kernel.exceptions import (
    InvalidRequestException,
    OperationTimeoutException,
    PyFlyException,
)
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.converters import build_exception_converter_service
from pyfly.web.cors import CORSConfig
from pyfly.web.params import Cookie, Header, QueryParam

# ---------------------------------------------------------------------------
# #206 — missing required scalar binding -> 400
# ---------------------------------------------------------------------------


def _get_request(query: bytes = b"", headers: list | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/x",
            "path_params": {},
            "query_string": query,
            "headers": headers or [],
        }
    )


class TestMissingRequiredParam:
    @pytest.mark.asyncio
    async def test_missing_required_query_param_raises_400(self):
        async def handler(self, q: QueryParam[str]):
            pass

        resolver = ParameterResolver(handler)
        assert resolver.params[0].required is True
        with pytest.raises(InvalidRequestException) as exc:
            await resolver.resolve(_get_request(query=b""))
        assert exc.value.code == "MISSING_PARAMETER"

    @pytest.mark.asyncio
    async def test_optional_query_param_returns_none(self):
        async def handler(self, q: QueryParam[int | None]):
            pass

        resolver = ParameterResolver(handler)
        assert resolver.params[0].required is False
        kwargs = await resolver.resolve(_get_request(query=b""))
        assert kwargs == {"q": None}

    @pytest.mark.asyncio
    async def test_defaulted_query_param_returns_default(self):
        async def handler(self, q: QueryParam[int] = 7):
            pass

        resolver = ParameterResolver(handler)
        assert resolver.params[0].required is False
        kwargs = await resolver.resolve(_get_request(query=b""))
        assert kwargs == {"q": 7}

    @pytest.mark.asyncio
    async def test_missing_required_header_raises_400(self):
        async def handler(self, x_api_key: Header[str]):
            pass

        resolver = ParameterResolver(handler)
        with pytest.raises(InvalidRequestException) as exc:
            await resolver.resolve(_get_request())
        assert "x-api-key" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_required_cookie_raises_400(self):
        async def handler(self, session: Cookie[str]):
            pass

        resolver = ParameterResolver(handler)
        with pytest.raises(InvalidRequestException):
            await resolver.resolve(_get_request())


# ---------------------------------------------------------------------------
# #204 — CORS auto-configuration from properties
# ---------------------------------------------------------------------------


class TestCorsAutoConfiguration:
    def test_from_config_disabled_returns_none(self):
        assert CORSConfig.from_config(Config({})) is None
        cfg = Config({"pyfly": {"web": {"cors": {"enabled": False}}}})
        assert CORSConfig.from_config(cfg) is None

    def test_from_config_builds_when_enabled(self):
        cfg = Config(
            {
                "pyfly": {
                    "web": {
                        "cors": {
                            "enabled": True,
                            "allowed-origins": ["https://a.example", "https://b.example"],
                            "allow-credentials": True,
                            "max-age": 1234,
                        }
                    }
                }
            }
        )
        cors = CORSConfig.from_config(cfg)
        assert cors is not None
        assert cors.allowed_origins == ["https://a.example", "https://b.example"]
        assert cors.allow_credentials is True
        assert cors.max_age == 1234
        # Unspecified methods fall back to Spring's permit-default set.
        assert cors.allowed_methods == ["GET", "HEAD", "POST"]

    def test_from_config_accepts_comma_separated_string(self):
        cfg = Config({"pyfly": {"web": {"cors": {"enabled": True, "allowed-origins": "https://x, https://y"}}}})
        cors = CORSConfig.from_config(cfg)
        assert cors is not None
        assert cors.allowed_origins == ["https://x", "https://y"]

    def test_create_app_auto_wires_cors_from_context(self):
        async def hello(request):
            return JSONResponse({"ok": True})

        ctx = ApplicationContext(
            Config({"pyfly": {"web": {"cors": {"enabled": True, "allowed-origins": ["https://allowed.example"]}}}})
        )
        app = create_app(context=ctx, extra_routes=[Route("/hello", hello)])
        client = TestClient(app)
        resp = client.get("/hello", headers={"Origin": "https://allowed.example"})
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.example"

    def test_create_app_no_cors_header_when_disabled(self):
        async def hello(request):
            return JSONResponse({"ok": True})

        ctx = ApplicationContext(Config({}))
        app = create_app(context=ctx, extra_routes=[Route("/hello", hello)])
        client = TestClient(app)
        resp = client.get("/hello", headers={"Origin": "https://anywhere.example"})
        assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# #202 — ExceptionConverterService wired into the global error path
# ---------------------------------------------------------------------------


class _DummyError(Exception):
    pass


class _DummyConverter:
    def can_handle(self, exc: Exception) -> bool:
        return isinstance(exc, _DummyError)

    def convert(self, exc: Exception) -> PyFlyException:
        return InvalidRequestException("dummy", code="DUMMY")


class TestExceptionConverterWiring:
    def test_builtin_converters_translate(self):
        svc = build_exception_converter_service()
        json_err = json.JSONDecodeError("bad", "{", 0)
        assert isinstance(svc.convert(json_err), InvalidRequestException)
        assert isinstance(svc.convert(TimeoutError("slow")), OperationTimeoutException)
        assert svc.convert(_DummyError()) is None

    @pytest.mark.asyncio
    async def test_user_converter_beans_are_discovered(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(_DummyConverter)
        await ctx.start()
        try:
            svc = build_exception_converter_service(ctx)
            converted = svc.convert(_DummyError())
            assert isinstance(converted, InvalidRequestException)
            assert converted.code == "DUMMY"
        finally:
            await ctx.stop()

    def test_global_handler_uses_converter_for_timeout(self):
        async def boom(request):
            raise TimeoutError("slow")

        app = create_app(extra_routes=[Route("/boom", boom)])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/boom")
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "OPERATION_TIMEOUT"

    def test_global_handler_falls_back_to_500_for_unknown(self):
        async def boom(request):
            raise RuntimeError("unexpected")

        app = create_app(extra_routes=[Route("/boom", boom)])
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/boom")
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
