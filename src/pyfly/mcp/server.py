# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""A public-SDK facade bridge into one tools/resources/prompts handler registry."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server.extension import Extension, compose_tool_call_handler
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.shared.exceptions import MCPError


class McpServer(MCPServer):
    """Keep SDK decorators and validation while exposing a public native registry.

    Uses only the SDK's public facade, Context, middleware and Extension APIs.
    No private SDK registry, session dictionary, or transport is inspected.
    Tasks, subscriptions, server-initiated requests and MRTR are outside this
    bounded JSON transport; existing private-review tools return normal results.
    """

    def __init__(self, name: str, *, extensions: Sequence[Extension] = (), **kwargs: Any) -> None:
        subscriptions = kwargs.pop("subscriptions", False)
        if subscriptions is not False:
            raise ValueError("Native portable JSON MCP does not support subscriptions")
        super().__init__(name, extensions=extensions, subscriptions=False, **kwargs)

        @asynccontextmanager
        async def lifecycle(_: Server[Any]) -> AsyncIterator[Any]:
            if self.settings.lifespan is None:
                yield {}
            else:
                async with self.settings.lifespan(self) as state:
                    yield state

        self.registry = Server(
            name,
            title=self.title,
            description=self.description,
            instructions=self.instructions,
            website_url=self.website_url,
            icons=self.icons,
            version=self.version,
            lifespan=lifecycle,
        )
        self.registry.add_request_handler("tools/list", types.PaginatedRequestParams, self._native_tools)
        self.registry.add_request_handler(
            "tools/call", types.CallToolRequestParams, compose_tool_call_handler(extensions, self._native_call)
        )
        self.registry.add_request_handler("resources/list", types.PaginatedRequestParams, self._native_resources)
        self.registry.add_request_handler("resources/read", types.ReadResourceRequestParams, self._native_read)
        self.registry.add_request_handler(
            "resources/templates/list", types.PaginatedRequestParams, self._native_templates
        )
        self.registry.add_request_handler("prompts/list", types.PaginatedRequestParams, self._native_prompts)
        self.registry.add_request_handler("prompts/get", types.GetPromptRequestParams, self._native_prompt)
        self.registry.middleware = self.middleware
        for extension in extensions:
            self.registry.extensions[extension.identifier] = extension.settings()
            for binding in extension.methods():

                async def handle(ctx: Any, params: Any, binding: Any = binding) -> Any:
                    if binding.protocol_versions is not None and ctx.protocol_version not in binding.protocol_versions:
                        raise MCPError(types.METHOD_NOT_FOUND, "Method not found")
                    return await binding.handler(ctx, params)

                self.registry.add_request_handler(binding.method, binding.params_type, handle)

    def _context(self, ctx: Any, params: Any) -> Context:
        return Context(request_context=ctx, mcp_server=self, input_params=params)

    @property
    def session_manager(self) -> Any:
        """The active SDK manager, whether served directly or through the native registry."""
        try:
            return super().session_manager
        except RuntimeError:
            return self.registry.session_manager

    @staticmethod
    def _complete(result: Any) -> Any:
        if isinstance(result, types.InputRequiredResult):
            raise MCPError(types.INVALID_REQUEST, "Multi-round-trip requests are not supported by portable JSON MCP")
        return result

    async def _native_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await self.list_tools())

    async def _native_call(self, ctx: Any, params: types.CallToolRequestParams) -> Any:
        try:
            return self._complete(await self.call_tool(params.name, params.arguments or {}, self._context(ctx, params)))
        except ToolError as error:
            return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=str(error))])

    async def _native_resources(self, ctx: Any, params: Any) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=await self.list_resources())

    async def _native_read(self, ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        try:
            values = self._complete(await self.read_resource(params.uri, self._context(ctx, params)))
        except ResourceError as error:
            raise MCPError(
                types.INVALID_PARAMS if isinstance(error, ResourceNotFoundError) else types.INTERNAL_ERROR,
                str(error),
                {"uri": str(params.uri)},
            ) from error
        content: list[types.TextResourceContents | types.BlobResourceContents] = []
        for item in values:
            if isinstance(item.content, bytes):
                content.append(
                    types.BlobResourceContents(
                        uri=params.uri,
                        blob=base64.b64encode(item.content).decode(),
                        mime_type=item.mime_type or "application/octet-stream",
                        _meta=item.meta,
                    )
                )
            else:
                content.append(
                    types.TextResourceContents(
                        uri=params.uri, text=item.content, mime_type=item.mime_type or "text/plain", _meta=item.meta
                    )
                )
        return types.ReadResourceResult(contents=content)

    async def _native_templates(self, ctx: Any, params: Any) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resource_templates=await self.list_resource_templates())

    async def _native_prompts(self, ctx: Any, params: Any) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=await self.list_prompts())

    async def _native_prompt(self, ctx: Any, params: types.GetPromptRequestParams) -> Any:
        return self._complete(await self.get_prompt(params.name, params.arguments, self._context(ctx, params)))
