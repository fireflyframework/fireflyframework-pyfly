"""Installed auto-configurations construct Agentic after its native resources."""

from pyfly.agentic.pipeline.durable import DurableRunStore
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.audit import AuditJournal
from pyfly.context import ApplicationContext
from pyfly.core import Config


async def test_discovered_durable_runtime_borrows_native_data_and_audit(tmp_path):
    from pyfly.data.relational.sqlalchemy import SqlAlchemyTransactionManager

    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {"enabled": True, "plugin-auto-discover": False, "durable": {"enabled": True}},
                    "audit": {"enabled": True},
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}",
                            "ddl-auto": "create",
                        }
                    },
                }
            }
        )
    )
    try:
        await context.start()
        store = context.get_bean(DurableRunStore)
        assert store.transaction_manager is context.get_bean(SqlAlchemyTransactionManager)
        assert store.audit is context.get_bean(AuditJournal)
    finally:
        await context.stop()


async def test_discovered_mongo_memory_borrows_native_client_without_connecting(monkeypatch):
    from unittest.mock import AsyncMock

    from pymongo import AsyncMongoClient
    from pymongo.asynchronous.collection import AsyncCollection

    from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager

    monkeypatch.setattr(AsyncCollection, "index_information", AsyncMock(return_value={}))
    monkeypatch.setattr(AsyncCollection, "create_indexes", AsyncMock(return_value=[]))
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "plugin-auto-discover": False,
                        "memory-backend": "mongodb",
                        "memory-create-indexes": True,
                    },
                    "data": {
                        "document": {"enabled": True, "uri": "mongodb://127.0.0.1:1", "options": {"connect": False}}
                    },
                }
            }
        )
    )
    try:
        await context.start()
        store = context.get_bean(AgenticRuntime).memory.store
        assert store._client is context.get_bean(AsyncMongoClient)
        assert store._manager is context.get_bean(MongoTransactionManager)
    finally:
        await context.stop()
