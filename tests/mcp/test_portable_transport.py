from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware

from pyfly.mcp import McpTransport
from pyfly.security.context import SecurityContext

APPS = "io.modelcontextprotocol/ui"
MIME = "text/html;profile=mcp-app"
HEADERS = {"Accept": "application/json, text/event-stream", "x-test-user": "alice"}


class Authentication(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.security_context = SecurityContext(user_id=user)
        return await call_next(request)


@pytest.mark.asyncio
async def test_legacy_initialize_on_a_reconstructs_public_sdk_context_on_b():
    from pyfly.mcp import InMemoryMcpSessionStore, McpSessionPrincipal

    store = InMemoryMcpSessionStore()
    seen = []

    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="review", inputSchema={"type": "object"})])

    async def call(ctx, params):
        seen.append((ctx.protocol_version, ctx.session.client_capabilities, ctx.request.state.security_context.user_id))
        return types.CallToolResult(content=[types.TextContent(type="text", text="review")])

    def principal(request, context):
        return McpSessionPrincipal(context.user_id, "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1))

    transports = [
        McpTransport.from_server(
            Server("test", on_list_tools=tools, on_call_tool=call),
            session_store=store,
            session_principal=principal,
            allowed_hosts=["test"],
        )
        for _ in range(2)
    ]
    async with AsyncExitStack() as stack:
        clients = []
        for transport in transports:
            app = Starlette(routes=[transport.route])
            app.add_middleware(Authentication)
            await stack.enter_async_context(transport.lifespan())
            clients.append(
                await stack.enter_async_context(
                    httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
                )
            )
        opened = await clients[0].post(
            "/mcp",
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "test", "version": "1"},
                    "capabilities": {"extensions": {APPS: {"mimeTypes": [MIME]}}},
                },
            },
        )
        assert opened.status_code == 200, opened.text
        session = opened.headers["mcp-session-id"]
        headers = {**HEADERS, "Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"}
        ready = await clients[1].post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert ready.status_code == 202, ready.text
        result = await clients[1].post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "review", "arguments": {}}},
        )
        assert result.status_code == 200, result.text
        assert result.json()["result"]["content"][0]["text"] == "review"
    assert len(seen) == 1 and seen[0][0] == "2025-11-25" and seen[0][2] == "alice"
    assert seen[0][1].extensions[APPS]["mimeTypes"] == [MIME]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "expired_principal",
        "revoked_resolver",
        "initialize_notification",
        "mixed_era",
        "duplicate_session",
        "modern_delete",
    ],
)
async def test_protocol_and_authority_rejections_precede_callbacks(case):
    from pyfly.mcp import InMemoryMcpSessionStore, McpSessionPrincipal

    calls = []

    async def tools(ctx, params):
        calls.append("tools")
        return types.ListToolsResult(tools=[])

    async def call(ctx, params):
        calls.append("call")
        return types.CallToolResult(content=[])

    def principal(request, context):
        if case == "revoked_resolver":
            from pyfly.kernel import UnauthorizedException

            raise UnauthorizedException("Revoked grant")
        return McpSessionPrincipal(
            context.user_id,
            "https://service.test/mcp",
            datetime.now(UTC) + timedelta(seconds=-1 if case == "expired_principal" else 3600),
        )

    transport = McpTransport.from_server(
        Server("test", on_list_tools=tools, on_call_tool=call),
        session_store=InMemoryMcpSessionStore(),
        session_principal=principal,
        allowed_hosts=["test"],
    )
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with (
        transport.lifespan(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        if case in {"expired_principal", "revoked_resolver"}:
            response = await client.post(
                "/mcp",
                headers={**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {types.PROTOCOL_VERSION_META_KEY: "2026-07-28", types.CLIENT_CAPABILITIES_META_KEY: {}}
                    },
                },
            )
            assert response.status_code == 401
        elif case == "initialize_notification":
            response = await client.post(
                "/mcp",
                headers=HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "clientInfo": {"name": "test", "version": "1"},
                        "capabilities": {},
                    },
                },
            )
            assert response.status_code == 400
        elif case == "mixed_era":
            response = await client.post(
                "/mcp",
                headers={**HEADERS, "MCP-Protocol-Version": "2025-11-25"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {types.PROTOCOL_VERSION_META_KEY: "2026-07-28", types.CLIENT_CAPABILITIES_META_KEY: {}}
                    },
                },
            )
            assert response.status_code == 400
        elif case == "duplicate_session":
            response = await client.post(
                "/mcp",
                headers=list(HEADERS.items()) + [("MCP-Session-Id", "a"), ("MCP-Session-Id", "b")],
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 400
        else:
            response = await client.delete(
                "/mcp", headers={**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Session-Id": "a"}
            )
            assert response.status_code == 405
    assert calls == []


@pytest.mark.asyncio
async def test_json_boundary_rejects_round_trip_state_without_exposing_it():
    from pyfly.mcp import InMemoryMcpSessionStore, McpSessionPrincipal

    async def call(ctx, params):
        return types.InputRequiredResult(request_state="private-round-trip-state-must-not-leak")

    server = Server("test", on_call_tool=call)
    transport = McpTransport.from_server(
        server,
        session_store=InMemoryMcpSessionStore(),
        session_principal=lambda request, context: McpSessionPrincipal(
            context.user_id, "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1)
        ),
        allowed_hosts=["test"],
    )
    app = Starlette(routes=[transport.route])
    app.add_middleware(Authentication)
    async with (
        transport.lifespan(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/mcp",
            headers={
                **HEADERS,
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "round-trip",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "round-trip",
                    "arguments": {},
                    "_meta": {types.PROTOCOL_VERSION_META_KEY: "2026-07-28", types.CLIENT_CAPABILITIES_META_KEY: {}},
                },
            },
        )
        assert response.status_code == 400, response.text
        assert "private-round-trip-state-must-not-leak" not in response.text
