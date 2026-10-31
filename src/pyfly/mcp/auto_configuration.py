# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in native MCP metadata persistence; unsupported backends fail closed."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean, conditional_on_property
from pyfly.core.config import Config
from pyfly.mcp.sessions import InMemoryMcpSessionStore, McpSessionStore
from pyfly.mcp.sql_sessions import SqlMcpSessionStore


@auto_configuration
@conditional_on_property("pyfly.mcp.sessions.enabled", having_value="true")
@conditional_on_missing_bean(McpSessionStore)
class McpSessionStoreAutoConfiguration:
    @bean
    def mcp_session_store(self, config: Config, container: Container | None = None) -> McpSessionStore:
        backend = str(config.get("pyfly.mcp.sessions.store", "memory")).strip().lower()
        if backend == "memory":
            return InMemoryMcpSessionStore()
        if backend != "sql":
            raise ValueError("pyfly.mcp.sessions.store must be memory or sql")
        from pyfly.data.relational.framework_schema import (
            context_datasource_registry,
            creates_tables,
            module_datasource,
        )

        registry = context_datasource_registry(config, container)
        datasource = module_datasource(registry, config, "pyfly.mcp.sessions", name="mcp-sessions")
        return SqlMcpSessionStore(datasource, create_table=creates_tables(registry.properties.ddl_auto))
