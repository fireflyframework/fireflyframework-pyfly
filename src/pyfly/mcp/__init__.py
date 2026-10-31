# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Optional MCP Streamable HTTP integration."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.mcp.server import McpServer
    from pyfly.web.adapters.starlette.mcp import McpTransport
from pyfly.mcp.sessions import InMemoryMcpSessionStore, McpSessionMetadata, McpSessionPrincipal, McpSessionStore
from pyfly.mcp.sql_sessions import SqlMcpSessionStore

__all__ = [
    "McpTransport",
    "McpServer",
    "InMemoryMcpSessionStore",
    "McpSessionMetadata",
    "McpSessionPrincipal",
    "McpSessionStore",
    "SqlMcpSessionStore",
]


def __getattr__(name: str) -> Any:
    if name == "McpTransport":
        from pyfly.web.adapters.starlette.mcp import McpTransport

        return McpTransport
    if name == "McpServer":
        from pyfly.mcp.server import McpServer

        return McpServer
    raise AttributeError(name)
