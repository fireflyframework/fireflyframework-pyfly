# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Memory adapters borrowing PyFly Data resources, sessions, and transaction boundaries.

Connection and pool settings belong to ``pyfly.data``. These adapters neither create another pool nor
close the supplied datasource/client. The historical schema/table/collection locations stay intact.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory._sync_bridge import ApplicationLoopStore
from pyfly.agentic.memory.relational_store import RelationalMemoryStore
from pyfly.agentic.memory.types import MemoryEntry


class PostgreSQLStore(RelationalMemoryStore):
    """Native relational memory at the existing PostgreSQL location.

    ``datasource`` is a PyFly DataSource, transaction manager, or registered datasource name.
    Legacy URL/pool arguments require migration to ``pyfly.data.relational`` configuration.
    """

    def __init__(
        self,
        datasource: object = None,
        *,
        schema_name: str = "firefly_memory",
        table: str = "memory_entries",
        create_table: bool = False,
        clock: Callable[[], datetime] | None = None,
        url: str | None = None,
        pool_size: int | None = None,
        pool_min_size: int | None = None,
        timeout: float | None = None,
    ) -> None:
        if url is not None or any(value is not None for value in (pool_size, pool_min_size, timeout)):
            raise ValueError("Move memory connection/pool settings to pyfly.data.relational and pass its datasource")
        super().__init__(datasource, table=table, schema=schema_name, create_table=create_table, clock=clock)


class MongoDBStore(ApplicationLoopStore):
    """Memory on the framework's native async Mongo client and document transaction manager.

    Initialize on the application's event loop before using synchronous methods from other threads.
    Database/collection names keep their historic defaults; the framework owns the client.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        manager: Any = None,
        database: str = "firefly_memory",
        collection: str = "entries",
        create_indexes: bool = True,
        clock: Callable[[], datetime] | None = None,
        url: str | None = None,
        pool_size: int | None = None,
    ) -> None:
        super().__init__()
        if isinstance(client, str) or url is not None or pool_size is not None:
            raise ValueError(
                "Move memory connection/pool settings to pyfly.data.document and pass its native client/manager"
            )
        if (
            not collection
            or collection.startswith(".")
            or collection.endswith(".")
            or ".." in collection
            or any(c in collection for c in "$\x00")
            or not database
            or any(c in database for c in '/\\.$" \x00')
        ):
            raise ValueError("Memory database and collection must be valid MongoDB names")
        from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager

        if manager is not None and not isinstance(manager, MongoTransactionManager):
            raise TypeError("MongoDBStore requires a pyfly.data document MongoTransactionManager")
        if client is None and manager is not None:
            client = manager.client
        if client is None:
            raise ValueError("Pass the pyfly.data.document native client or transaction manager")
        if manager is None:
            manager = MongoTransactionManager.for_client(client)
        if not manager.owns(client):
            raise ValueError("The pyfly.data document manager does not serve the supplied MongoDB client")
        self._client = client
        self._manager = manager
        self.database = database
        self._collection_name = collection
        self._create_indexes = create_indexes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    @property
    def _collection(self) -> Any:
        from bson.codec_options import CodecOptions

        return self._client.get_database(
            self.database, codec_options=CodecOptions(tz_aware=True, tzinfo=UTC)
        ).get_collection(self._collection_name)

    def _joined_unit(self) -> Any:
        from pyfly.data.transaction.context import current_state
        from pyfly.data.transaction.errors import IllegalTransactionStateError

        owns_client = self._manager.owns(self._client)
        joined = current_state().target(self._manager.datasource)
        if joined is not None and (joined.manager is not self._manager or not owns_client):
            raise IllegalTransactionStateError(
                "The active unit belongs to another application's document memory datasource",
                datasource=self._manager.datasource,
            )
        return joined

    async def initialize(self) -> None:
        self._check_loop(initialize=True)
        self._joined_unit()
        async with self._initialization_lock:
            if self._initialized:
                return
            from pymongo import IndexModel

            collection = self._collection
            indexes = await collection.index_information()
            for name, info in indexes.items():
                if info.get("unique") and info["key"] == [("entry_id", 1)]:
                    raise DatabaseStoreError(
                        f"Memory index {name!r} needs an explicit migration from unique entry_id to "
                        "unique (namespace, entry_id); no index or document was removed"
                    )
            wanted = [
                IndexModel([("namespace", 1), ("entry_id", 1)], name="namespace_entry_id", unique=True),
                IndexModel(
                    [("namespace", 1), ("key", 1), ("created_at", -1), ("entry_id", -1)], name="namespace_key_created"
                ),
                IndexModel([("expires_at", 1)], name="memory_expires_at"),
            ]
            if self._create_indexes:
                await collection.create_indexes(wanted)
            else:
                for index in wanted:
                    expected = index.document
                    found = indexes.get(expected["name"])
                    if (
                        found is None
                        or list(expected["key"].items()) != found["key"]
                        or bool(expected.get("unique")) != bool(found.get("unique"))
                    ):
                        raise DatabaseStoreError(
                            f"Memory index {expected['name']!r} is missing or incompatible; create it with a migration"
                        )
            self._initialized = True

    async def start(self) -> None:
        await self.initialize()

    async def close(self) -> None:
        self._check_loop()
        self._initialized = False

    async def stop(self) -> None:
        await self.close()

    @contextlib.asynccontextmanager
    async def _unit(self, *, read_only: bool = False, single: bool = True) -> AsyncIterator[Any]:
        from pyfly.data.document.mongodb.transaction_manager import in_transaction
        from pyfly.data.transaction.errors import IllegalTransactionStateError
        from pyfly.data.transaction.template import AutoUnit

        manager = self._manager
        joined = self._joined_unit()
        if joined is not None:
            joined.check_usable()
            if not read_only and joined.read_only:
                raise IllegalTransactionStateError("The document unit is read-only; memory cannot write in it")
            if not single and not in_transaction(joined):
                raise IllegalTransactionStateError("Replacing a memory key requires a document transaction")
            yield joined
            return
        if not single and not await manager.supports_transactions():
            raise IllegalTransactionStateError("Replacing a memory key requires a MongoDB replica-set transaction")
        async with AutoUnit(manager, read_only=read_only, autocommit=single) as unit:
            yield unit

    def _values(self, namespace: str, entry: MemoryEntry) -> dict[str, Any]:
        values = entry.model_dump(mode="json")
        values.update(namespace=namespace, created_at=entry.created_at, expires_at=entry.expires_at)
        return values

    def _visible(self, namespace: str) -> dict[str, Any]:
        return {"namespace": namespace, "$or": [{"expires_at": None}, {"expires_at": {"$gt": self._clock()}}]}

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        await self.initialize()
        async with self._unit() as unit, unit.operation():
            await self._collection.update_one(
                {"namespace": namespace, "entry_id": entry.entry_id},
                {"$set": self._values(namespace, entry)},
                upsert=True,
                session=unit.resource,
            )

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        await self.initialize()
        async with self._unit(read_only=True) as unit, unit.operation():
            documents = (
                await self._collection.find(self._visible(namespace), session=unit.resource)
                .sort([("created_at", 1), ("entry_id", 1)])
                .to_list(length=None)
            )
        return [MemoryEntry.model_validate(document) for document in documents]

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        await self.initialize()
        async with self._unit(read_only=True) as unit, unit.operation():
            document = await self._collection.find_one(
                {**self._visible(namespace), "key": key},
                session=unit.resource,
                sort=[("created_at", -1), ("entry_id", -1)],
            )
        return MemoryEntry.model_validate(document) if document is not None else None

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        await self.initialize()
        async with self._unit() as unit, unit.operation():
            await self._collection.delete_one({"namespace": namespace, "entry_id": entry_id}, session=unit.resource)

    async def async_clear(self, namespace: str) -> None:
        await self.initialize()
        async with self._unit() as unit, unit.operation():
            await self._collection.delete_many({"namespace": namespace}, session=unit.resource)

    async def async_replace(self, namespace: str, entry: MemoryEntry) -> None:
        """Replace a key in the caller's transaction, or a short transaction of its own."""
        await self.initialize()
        if entry.key is None:
            raise ValueError("A replacement memory entry requires a key")
        async with self._unit(single=False) as unit, unit.guard:
            async with unit.operation():
                await self._collection.delete_many({"namespace": namespace, "key": entry.key}, session=unit.resource)
            async with unit.operation():
                await self._collection.update_one(
                    {"namespace": namespace, "entry_id": entry.entry_id},
                    {"$set": self._values(namespace, entry)},
                    upsert=True,
                    session=unit.resource,
                )

    async def cleanup_expired(self) -> int:
        await self.initialize()
        async with self._unit() as unit, unit.operation():
            result = await self._collection.delete_many({"expires_at": {"$lte": self._clock()}}, session=unit.resource)
            return int(result.deleted_count)
