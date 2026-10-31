from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mcp import types
from mcp.server.extension import Extension
from mcp.server.mcpserver import Context
from starlette.applications import Starlette

from tests.mcp.test_portable_transport import APPS, HEADERS, MIME, Authentication


@pytest.mark.asyncio
async def test_public_facade_preserves_extensions_resources_prompts_and_metadata():
    from pyfly.mcp import InMemoryMcpSessionStore, McpServer, McpSessionPrincipal, McpTransport

    called = []

    class Review(Extension):
        identifier = APPS

        def settings(self):
            return {"mimeTypes": [MIME]}

        async def intercept_tool_call(self, params, ctx, call_next):
            caps = ctx.session.client_capabilities
            if not caps or APPS not in (caps.extensions or {}):
                return types.CallToolResult(
                    is_error=True, content=[types.TextContent(type="text", text="Apps required")]
                )
            return await call_next(ctx)

    facade = McpServer("test", extensions=[Review()])

    @facade.tool(meta={"private_review": True}, structured_output=True)
    def review(ctx: Context) -> dict[str, str]:
        called.append(ctx.request_context.session.client_capabilities)
        return {"nonce": "private-only-after-capability-gate"}

    @facade.resource("ui://review", mime_type=MIME, meta={"ui": {"csp": {"connectDomains": []}}})
    def resource():
        return "<html>Review</html>"

    @facade.prompt()
    def review_help():
        return "Review sources and approve explicitly."

    transport = McpTransport.from_server(
        facade.registry,
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

        async def call(method, params):
            meta = {
                types.PROTOCOL_VERSION_META_KEY: "2026-07-28",
                types.CLIENT_CAPABILITIES_META_KEY: {"extensions": {APPS: {"mimeTypes": [MIME]}}},
            }
            headers = {**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
            name = params.get("name") or params.get("uri")
            if name:
                headers["Mcp-Name"] = name
            return await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}},
            )

        tools = await call("tools/list", {})
        assert tools.status_code == 200 and tools.json()["result"]["tools"][0]["_meta"]["private_review"]
        reviewed = await call("tools/call", {"name": "review", "arguments": {}})
        assert reviewed.status_code == 200 and "structuredContent" in reviewed.json()["result"], reviewed.text
        assert reviewed.json()["result"]["structuredContent"]["nonce"]
        resources = await call("resources/list", {})
        assert resources.json()["result"]["resources"][0]["_meta"]["ui"]["csp"] == {"connectDomains": []}
        result = await call("resources/read", {"uri": "ui://review"})
        assert result.json()["result"]["contents"][0]["mimeType"] == MIME
        prompts = await call("prompts/list", {})
        assert prompts.json()["result"]["prompts"][0]["name"] == "review_help"
        result = await call("prompts/get", {"name": "review_help"})
        assert "approve explicitly" in result.json()["result"]["messages"][0]["content"]["text"]
        denied = await client.post(
            "/mcp",
            headers={**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "review"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "review",
                    "arguments": {},
                    "_meta": {types.PROTOCOL_VERSION_META_KEY: "2026-07-28", types.CLIENT_CAPABILITIES_META_KEY: {}},
                },
            },
        )
        assert denied.json()["result"]["isError"] and "nonce" not in denied.text
    assert len(called) == 1


@pytest.mark.asyncio
async def test_modern_capabilities_are_per_request_across_independent_replicas():
    from contextlib import AsyncExitStack

    from pyfly.mcp import InMemoryMcpSessionStore, McpServer, McpSessionPrincipal, McpTransport

    seen = []
    store = InMemoryMcpSessionStore()
    servers = [McpServer("replica") for _ in range(2)]
    for server in servers:

        @server.tool()
        def capability(ctx: Context) -> str:
            caps = ctx.request_context.session.client_capabilities
            declared = APPS in (caps.extensions or {}) if caps else False
            seen.append(declared)
            return "apps" if declared else "no-apps"

    async with AsyncExitStack() as stack:
        clients = []
        for server in servers:
            transport = McpTransport.from_server(
                server.registry,
                session_store=store,
                session_principal=lambda request, context: McpSessionPrincipal(
                    context.user_id, "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1)
                ),
                allowed_hosts=["test"],
            )
            app = Starlette(routes=[transport.route])
            app.add_middleware(Authentication)
            await stack.enter_async_context(transport.lifespan())
            clients.append(
                await stack.enter_async_context(
                    httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
                )
            )
        for client, apps in ((clients[0], True), (clients[1], True), (clients[1], False), (clients[0], False)):
            response = await client.post(
                "/mcp",
                headers={
                    **HEADERS,
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "capability",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "capability",
                        "arguments": {},
                        "_meta": {
                            types.PROTOCOL_VERSION_META_KEY: "2026-07-28",
                            types.CLIENT_CAPABILITIES_META_KEY: {"extensions": {APPS: {"mimeTypes": [MIME]}}}
                            if apps
                            else {},
                        },
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["result"]["content"][0]["text"] == ("apps" if apps else "no-apps")
    assert seen == [True, True, False, False]
    assert store._rows == {}


@pytest.mark.asyncio
async def test_official_legacy_client_preserves_apps_with_json_and_get_405():
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from pyfly.mcp import InMemoryMcpSessionStore, McpServer, McpSessionPrincipal, McpTransport

    server = McpServer("legacy-client")

    @server.tool()
    def declared(ctx: Context) -> str:
        caps = ctx.request_context.session.client_capabilities
        return caps.extensions[APPS]["mimeTypes"][0]

    transport = McpTransport.from_server(
        server.registry,
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
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app), headers={"x-test-user": "alice"}) as http,
        streamable_http_client("http://test/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1], extensions={APPS: {"mimeTypes": [MIME]}}) as client,
    ):
        await client.initialize()
        result = await client.call_tool("declared", {})
        assert result.content[0].text == MIME
