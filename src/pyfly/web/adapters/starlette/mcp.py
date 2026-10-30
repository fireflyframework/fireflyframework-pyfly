# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Request-scoped MCP 2026 transport for native PyFly web applications."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder

ListTools = Callable[[SecurityContext, types.PaginatedRequestParams | None], Awaitable[types.ListToolsResult]]
CallTool = Callable[[SecurityContext, types.CallToolRequestParams], Awaitable[types.CallToolResult]]


class McpTransport:
    """Compose an SDK transport with the host's PyFly security filters and lifespan.

    Authentication and resource metadata remain the host application's responsibility.
    Callbacks receive only the authenticated context, never the bearer token.
    """

    def __init__(
        self,
        name: str,
        *,
        list_tools: ListTools,
        call_tool: CallTool,
        path: str = "/mcp",
        max_request_body_size: int = 65536,
        allowed_hosts: Sequence[str],
        allowed_origins: Sequence[str] = (),
    ) -> None:
        if not path.startswith("/") or path == "/" or path.endswith("/"):
            raise ValueError("MCP path must be an absolute non-root path without a trailing slash")
        if not allowed_hosts or max_request_body_size <= 0:
            raise ValueError("MCP requires allowed hosts and a positive request body limit")
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._server: Server[Any] = Server(name, on_list_tools=self._list, on_call_tool=self._call)
        self._app = self._server.streamable_http_app(
            streamable_http_path=path,
            json_response=True,
            stateless_http=True,
            max_request_body_size=max_request_body_size,
            transport_security=TransportSecuritySettings(
                allowed_hosts=list(allowed_hosts), allowed_origins=list(allowed_origins)
            ),
        )
        self.route = Route(path, self)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self._server.session_manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.method != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
        context = getattr(request.state, "security_context", None)
        if not isinstance(context, SecurityContext) or not context.is_authenticated:
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        if request.headers.get("mcp-protocol-version") != "2026-07-28":
            await Response(status_code=400)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    @staticmethod
    def _context(ctx: ServerRequestContext[Any, Any]) -> SecurityContext:
        request = ctx.request
        context = getattr(getattr(request, "state", None), "security_context", None)
        if not isinstance(context, SecurityContext) or not context.is_authenticated:
            raise PermissionError("Authentication required")
        return context

    async def _list(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        context = self._context(ctx)
        with SecurityContextHolder.using(context):
            return await self._list_tools(context, params)

    async def _call(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        context = self._context(ctx)
        with SecurityContextHolder.using(context):
            return await self._call_tool(context, params)
