from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp import types
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware

from pyfly.mcp import McpTransport
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder


class Authentication(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.security_context = SecurityContext(user_id=user)
        return await call_next(request)


def request(method, name=None):
    params = {
        "_meta": {
            types.PROTOCOL_VERSION_META_KEY: "2026-07-28",
            types.CLIENT_INFO_META_KEY: {"name": "test", "version": "1"},
            types.CLIENT_CAPABILITIES_META_KEY: {},
        }
    }
    headers = {
        "Mcp-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
    }
    if name:
        params.update(name=name, arguments={})
        headers["Mcp-Name"] = name
    return headers, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


@pytest.mark.asyncio
async def test_request_context_isolation_and_protocol_guard():
    seen = []

    async def list_tools(context, params):
        await asyncio.sleep(0)
        seen.append(SecurityContextHolder.get_context().user_id)
        return types.ListToolsResult(tools=[types.Tool(name=context.user_id, inputSchema={"type": "object"})])

    async def call_tool(context, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text=context.user_id)])

    transport = McpTransport("test", list_tools=list_tools, call_tool=call_tool, allowed_hosts=["test"])
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with transport.lifespan():  # noqa: SIM117
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers, body = request("tools/list")

            async def listing(user):
                response = await client.post("/mcp", headers={**headers, "x-test-user": user}, json=body)
                assert response.status_code == 200, response.text
                return response.json()["result"]["tools"][0]["name"]

            assert await asyncio.gather(listing("alice"), listing("bob")) == ["alice", "bob"]
            assert sorted(seen) == ["alice", "bob"]
            assert (await client.post("/mcp", headers=headers, json=body)).status_code == 401
            assert (
                await client.post(
                    "/mcp", headers={**headers, "x-test-user": "alice", "Mcp-Protocol-Version": "2025-11-25"}, json=body
                )
            ).status_code == 400
            assert (await client.get("/mcp", headers={"x-test-user": "alice"})).status_code == 405


@pytest.mark.asyncio
async def test_official_sdk_client_round_trip():
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool(name="echo", inputSchema={"type": "object"})])

    async def call_tool(context, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text=context.user_id)])

    transport = McpTransport("test", list_tools=list_tools, call_tool=call_tool, allowed_hosts=["test"])
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with (
        transport.lifespan(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"x-test-user": "alice"}) as http,
        Client(streamable_http_client("http://test/mcp", http_client=http), mode="2026-07-28") as client,
    ):
        result = await client.list_tools()
        assert [item.name for item in result.tools] == ["echo"]
        result = await client.call_tool("echo", {})
        assert result.content[0].text == "alice"


@pytest.mark.asyncio
async def test_host_origin_and_body_limits():
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[])

    async def call_tool(context, params):
        return types.CallToolResult(content=[])

    transport = McpTransport(
        "test",
        list_tools=list_tools,
        call_tool=call_tool,
        allowed_hosts=["test"],
        allowed_origins=["https://allowed.test"],
        max_request_body_size=512,
    )
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with transport.lifespan():  # noqa: SIM117
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers, body = request("tools/list")
            headers["x-test-user"] = "alice"
            assert (
                await client.post("/mcp", headers={**headers, "origin": "https://evil.test"}, json=body)
            ).status_code == 403
            assert (await client.post("/mcp", headers={**headers, "host": "evil.test"}, json=body)).status_code == 421
            assert (await client.post("/mcp", headers=headers, content=b"x" * 513)).status_code == 413


@pytest.mark.asyncio
async def test_native_web_filter_context_reaches_sdk_callbacks():
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.kernel.exceptions import SecurityException
    from pyfly.security.http_security import HttpSecurity
    from pyfly.web.adapters.starlette.app import create_app
    from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter

    class Validator:
        def to_security_context(self, token):
            if token != "valid":
                raise SecurityException("invalid")
            return SecurityContext(user_id="machine")

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool(name=context.user_id, inputSchema={"type": "object"})])

    async def call_tool(context, params):
        return types.CallToolResult(content=[])

    transport = McpTransport("test", list_tools=list_tools, call_tool=call_tool, allowed_hosts=["test"])
    context = ApplicationContext(Config({}))
    context.container.register_instance(
        OAuth2ResourceServerFilter, OAuth2ResourceServerFilter(Validator(), error_mode="401")
    )
    policy = HttpSecurity()
    policy.authorize_requests().request_matchers("/mcp").authenticated()
    gate = policy.build()
    context.container.register_instance(type(gate), gate)
    app = create_app(context=context, extra_routes=[transport.route], docs_enabled=False)
    async with transport.lifespan():  # noqa: SIM117
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers, body = request("tools/list")
            assert (await client.post("/mcp", headers=headers, json=body)).status_code == 401
            assert (
                await client.post("/mcp", headers={**headers, "authorization": "Bearer bad"}, json=body)
            ).status_code == 401
            response = await client.post("/mcp", headers={**headers, "authorization": "Bearer valid"}, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["result"]["tools"][0]["name"] == "machine"


@pytest.mark.asyncio
async def test_configured_path_works_inside_native_app():
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[])

    async def call_tool(context, params):
        return types.CallToolResult(content=[])

    transport = McpTransport(
        "test", list_tools=list_tools, call_tool=call_tool, path="/api/v1/mcp", allowed_hosts=["test"]
    )
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with transport.lifespan():  # noqa: SIM117
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers, body = request("tools/list")
            response = await client.post("/api/v1/mcp", headers={**headers, "x-test-user": "alice"}, json=body)
            assert response.status_code == 200, response.text
