from __future__ import annotations

import pytest

from pyfly.core.config import Config


@pytest.mark.asyncio
async def test_sql_store_uses_native_datasource_and_respects_schema_validation(tmp_path):
    from pyfly.container.container import Container
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import FrameworkSchemaError
    from pyfly.mcp import SqlMcpSessionStore
    from pyfly.mcp.auto_configuration import McpSessionStoreAutoConfiguration

    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}",
                        "ddl-auto": "validate",
                    }
                },
                "mcp": {"sessions": {"enabled": True, "store": "sql"}},
            }
        }
    )
    registry = DataSourceRegistry.for_config(config)
    container = Container()
    container.register_instance(DataSourceRegistry, registry)
    try:
        store = McpSessionStoreAutoConfiguration().mcp_session_store(config, container)
        assert isinstance(store, SqlMcpSessionStore)
        assert store.datasource is registry.primary
        with pytest.raises(FrameworkSchemaError):
            await store.start()
    finally:
        await registry.close()


def test_unknown_store_does_not_fall_back_to_memory():
    from pyfly.mcp.auto_configuration import McpSessionStoreAutoConfiguration

    with pytest.raises(ValueError, match="store"):
        McpSessionStoreAutoConfiguration().mcp_session_store(
            Config({"pyfly": {"mcp": {"sessions": {"store": "misspelled"}}}})
        )
