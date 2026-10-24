# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Memory entries in the application's PyFly relational datasource and unit of work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pyfly.agentic.memory._sync_bridge import ApplicationLoopStore
from pyfly.agentic.memory.types import MemoryEntry


class RelationalMemoryStore(ApplicationLoopStore):
    """Borrow a PyFly datasource; transactions and pool ownership stay with PyFly Data.

    Database URLs and pool settings belong to ``pyfly.data.relational``. Initialize on the application's
    event loop before calling synchronous methods from other threads. No operation disposes the datasource.
    """

    def __init__(
        self,
        datasource: object,
        *,
        table: str = "pyfly_agentic_memory",
        schema: str | None = None,
        create_table: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        if isinstance(datasource, str) and "://" in datasource:
            raise ValueError(
                "Configure pyfly.data.relational and pass its datasource or transaction manager, not a URL"
            )
        from pyfly.data.relational.framework_schema import agentic_memory_table

        self._target = datasource
        self._manager: Any = None
        self._table = agentic_memory_table(table, schema=schema)
        self._create_table = create_table
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    @property
    def engine(self) -> Any:
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._transaction_manager())

    def _transaction_manager(self) -> Any:
        from pyfly.data.transaction.context import current_state
        from pyfly.data.transaction.errors import IllegalTransactionStateError
        from pyfly.data.transaction.registry import resolve_manager

        if self._manager is None:
            self._manager = resolve_manager(self._target)
        manager = self._manager
        joined = current_state().target(manager.datasource)
        if joined is not None and joined.manager is not manager:
            raise IllegalTransactionStateError(
                "The active unit belongs to another application's memory datasource", datasource=manager.datasource
            )
        return manager

    async def initialize(self) -> None:
        self._check_loop(initialize=True)
        manager = self._transaction_manager()
        async with self._initialization_lock:
            if self._initialized:
                return
            from sqlalchemy import inspect
            from sqlalchemy.schema import CreateSchema

            from pyfly.data.relational.framework_schema import FrameworkSchemaError, ensure_tables

            def check_identity(connection: Any) -> None:
                inspector = inspect(connection)
                table = self._table
                if not inspector.has_table(table.name, schema=table.schema):
                    return
                primary = set(inspector.get_pk_constraint(table.name, schema=table.schema)["constrained_columns"])
                unique = inspector.get_unique_constraints(table.name, schema=table.schema)
                indexes = inspector.get_indexes(table.name, schema=table.schema)
                global_id = any(
                    set(constraint.get("column_names") or ()) == {"entry_id"}
                    for constraint in [*unique, *(index for index in indexes if index.get("unique"))]
                )
                if primary != {"namespace", "entry_id"} or global_id:
                    raise FrameworkSchemaError(
                        f"Memory table {table.fullname} needs an explicit migration to PRIMARY KEY "
                        "(namespace, entry_id), removing global entry_id uniqueness; existing rows were not changed"
                    )

            async with self.engine.begin() as connection:
                await connection.run_sync(check_identity)
                if self._create_table and self._table.schema is not None:
                    await connection.execute(CreateSchema(self._table.schema, if_not_exists=True))
            await ensure_tables(manager, self._table, create=self._create_table)
            self._initialized = True

    async def start(self) -> None:
        await self.initialize()

    async def close(self) -> None:
        self._check_loop()
        self._initialized = False

    async def stop(self) -> None:
        await self.close()

    def _values(self, namespace: str, entry: MemoryEntry) -> dict[str, Any]:
        values = entry.model_dump(mode="json")
        # The historical PostgreSQL content column contains the complete entry.
        values["content"] = entry.model_dump(mode="json")
        values.update(namespace=namespace, created_at=entry.created_at, expires_at=entry.expires_at)
        return values

    def _visible(self, namespace: str) -> Any:
        from sqlalchemy import and_, or_

        columns = self._table.c
        return and_(
            columns.namespace == namespace, or_(columns.expires_at.is_(None), columns.expires_at > self._clock())
        )

    @staticmethod
    def _entry(row: Mapping[str, Any]) -> MemoryEntry:
        return MemoryEntry.model_validate(row["content"])

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        await self.initialize()
        from pyfly.data.relational.upsert import backend_name, native_upsert, upsert
        from pyfly.data.transaction import infrastructure_unit

        async with infrastructure_unit(
            self._transaction_manager(), single_statement=native_upsert(backend_name(self.engine))
        ) as session:
            await upsert(session, self._table, self._values(namespace, entry), key=["namespace", "entry_id"])

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        await self.initialize()
        from sqlalchemy import select

        from pyfly.data.transaction import infrastructure_unit

        table = self._table
        statement = select(table).where(self._visible(namespace)).order_by(table.c.created_at, table.c.entry_id)
        async with infrastructure_unit(self._transaction_manager(), read_only=True, single_statement=True) as session:
            return [self._entry(row) for row in (await session.execute(statement)).mappings()]

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        await self.initialize()
        from sqlalchemy import select

        from pyfly.data.transaction import infrastructure_unit

        table = self._table
        statement = (
            select(table)
            .where(self._visible(namespace), table.c.key == key)
            .order_by(table.c.created_at.desc(), table.c.entry_id.desc())
            .limit(1)
        )
        async with infrastructure_unit(self._transaction_manager(), read_only=True, single_statement=True) as session:
            row = (await session.execute(statement)).mappings().first()
        return self._entry(row) if row is not None else None

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        await self.initialize()
        from sqlalchemy import delete

        from pyfly.data.transaction import infrastructure_unit

        table = self._table
        async with infrastructure_unit(self._transaction_manager(), single_statement=True) as session:
            await session.execute(delete(table).where(table.c.namespace == namespace, table.c.entry_id == entry_id))

    async def async_clear(self, namespace: str) -> None:
        await self.initialize()
        from sqlalchemy import delete

        from pyfly.data.transaction import infrastructure_unit

        async with infrastructure_unit(self._transaction_manager(), single_statement=True) as session:
            await session.execute(delete(self._table).where(self._table.c.namespace == namespace))

    async def async_replace(self, namespace: str, entry: MemoryEntry) -> None:
        """Replace a working key in one transaction, joining the caller's transaction when present."""
        await self.initialize()
        if entry.key is None:
            raise ValueError("A replacement memory entry requires a key")
        from sqlalchemy import delete

        from pyfly.data.relational.upsert import upsert
        from pyfly.data.transaction import infrastructure_unit
        from pyfly.data.transaction.context import current_state
        from pyfly.data.transaction.errors import IllegalTransactionStateError

        manager = self._transaction_manager()
        joined = current_state().target(manager.datasource)
        if joined is not None and joined.autocommit:
            raise IllegalTransactionStateError("Replacing a memory key requires a transaction, not an autocommit unit")
        table = self._table
        async with infrastructure_unit(manager) as session:
            unit = current_state().target(manager.datasource)
            assert unit is not None
            async with unit.guard:
                await session.execute(delete(table).where(table.c.namespace == namespace, table.c.key == entry.key))
                await upsert(session, table, self._values(namespace, entry), key=["namespace", "entry_id"])

    async def cleanup_expired(self) -> int:
        await self.initialize()
        from sqlalchemy import delete

        from pyfly.data.transaction import infrastructure_unit

        async with infrastructure_unit(self._transaction_manager(), single_statement=True) as session:
            result = await session.execute(delete(self._table).where(self._table.c.expires_at <= self._clock()))
            return int(result.rowcount)
