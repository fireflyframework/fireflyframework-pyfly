# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""pgvector vector store backend.

A PostgreSQL-backed vector store using the ``pgvector`` extension, peer to the
Chroma / Pinecone / Qdrant adapters. Co-locating the vector projection with a
PostgreSQL instance means production deployments don't have to operate a
separate vector database.

Like the other adapters this is **namespace-based** (single ``namespace`` per
document); multi-tenant isolation is layered on top by
:class:`~pyfly.agentic.vectorstores.scoped.TenantScopedVectorStore`.

The store borrows a PyFly Data datasource and uses one table (``vector_documents`` by default)::

    CREATE TABLE <table> (
        id         TEXT PRIMARY KEY,
        namespace  TEXT NOT NULL DEFAULT 'default',
        embedding  vector(<dimension>) NOT NULL,
        text       TEXT NOT NULL,
        metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

ANN search uses an HNSW index over cosine distance. Data owns connections and transactions. :meth:`_prepare_session` is
an overridable per-transaction hook (default no-op) for callers that need
connection-level session setup -- e.g. ``SET LOCAL`` for Postgres Row-Level
Security GUCs.

Requires the ``agentic-vectorstores-pgvector`` extra
(``pip install pyfly[agentic-vectorstores-pgvector]``) and the
``pgvector`` extension available on the Postgres server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from pyfly.agentic.exceptions import VectorStoreError
from pyfly.agentic.vectorstores.base import BaseVectorStore
from pyfly.agentic.vectorstores.types import SearchFilter, SearchResult, VectorDocument

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# SQL comparison operator for each non-membership SearchFilter operator.
_SCALAR_OPERATORS = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}


class PgVectorVectorStore(BaseVectorStore):
    """Vectors on a borrowed PyFly relational datasource and its native unit of work.

    ``datasource`` accepts a DataSource, transaction manager, or registered datasource name.
    URLs and pool options belong to ``pyfly.data.relational``. ``create_schema=None`` follows
    the datasource's ``ddl-auto`` policy; explicit ``True`` creates missing vector objects and
    ``False`` validates them. Initialize at application startup before business transactions.
    No lifecycle method disposes the borrowed engine or commits a caller's transaction.
    """

    def __init__(
        self,
        datasource: object = None,
        *,
        dimension: int,
        table_name: str = "vector_documents",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        hnsw_ef_search: int = 200,
        pool_min_size: int | None = None,
        pool_max_size: int | None = None,
        embedder: Any | None = None,
        create_schema: bool | None = None,
        url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(embedder=embedder, **kwargs)
        if (
            url is not None
            or pool_min_size is not None
            or pool_max_size is not None
            or (isinstance(datasource, str) and "://" in datasource)
        ):
            raise ValueError("Move vector connection/pool settings to pyfly.data.relational and pass its datasource")
        for name, value in (
            ("dimension", dimension),
            ("hnsw_m", hnsw_m),
            ("hnsw_ef_construction", hnsw_ef_construction),
            ("hnsw_ef_search", hnsw_ef_search),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not _SAFE_IDENTIFIER.match(table_name):
            raise ValueError(f"Invalid table_name: {table_name!r}. Must be a valid SQL identifier.")
        self._target = datasource
        self._manager: Any = None
        self._dimension = dimension
        self._table = table_name
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._hnsw_ef_search = hnsw_ef_search
        self._initialised = False
        self._create_schema_enabled = create_schema
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._initialization_lock = asyncio.Lock()

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None and self._owner_loop is not loop:
            raise VectorStoreError("Vector data resources belong to another application event loop")

    async def initialise(self) -> None:
        """Create or validate vector objects on the owning loop; never open another pool."""
        self._check_loop()
        if self._owner_loop is None:
            self._owner_loop = asyncio.get_running_loop()
        if self._initialised:
            return
        from pyfly.data.relational.framework_schema import framework_engine
        from pyfly.data.transaction.context import current_state
        from pyfly.data.transaction.registry import resolve_manager

        manager = self._manager or resolve_manager(self._target)
        self._manager = manager
        if current_state().target(manager.datasource) is not None:
            raise VectorStoreError("Call 'await store.initialise()' at startup before entering a business transaction")
        engine = framework_engine(manager)
        if engine.dialect.name != "postgresql":
            raise VectorStoreError("PgVectorVectorStore requires a PostgreSQL PyFly datasource")
        async with self._initialization_lock:
            if self._initialised:
                return
            async with engine.begin() as connection:
                await self._create_schema(_DataConnection(connection))
            self._initialised = True

    async def start(self) -> None:
        await self.initialise()

    async def close(self) -> None:
        """Reset local startup state; the datasource registry owns the engine."""
        self._check_loop()
        self._initialised = False

    async def stop(self) -> None:
        await self.close()

    @property
    def create_schema(self) -> bool:
        """Effective vector schema creation policy (otherwise migration-only validation)."""
        if self._create_schema_enabled is not None:
            return self._create_schema_enabled
        from pyfly.data.relational.framework_schema import creates_tables

        target = self._manager or self._target
        if target is None or isinstance(target, str):
            from pyfly.data.transaction.registry import resolve_manager

            target = resolve_manager(target)
        target = getattr(target, "data_source", None) or target
        registry = getattr(target, "registry", None)
        properties = registry.properties if registry is not None else getattr(target, "properties", None)
        return creates_tables(getattr(properties, "ddl_auto", None))

    @contextlib.asynccontextmanager
    async def _operation(self, *, namespace: str, read_only: bool = False) -> AsyncIterator[_DataConnection]:
        await self.initialise()
        from pyfly.data.transaction import infrastructure_unit
        from pyfly.data.transaction.context import current_state
        from pyfly.data.transaction.errors import IllegalTransactionStateError

        manager = self._manager
        joined = current_state().target(manager.datasource)
        if joined is not None:
            if joined.manager is not manager:
                raise VectorStoreError("The active unit belongs to another application's datasource")
            if joined.read_only and not read_only:
                raise IllegalTransactionStateError(
                    "Cannot write vectors inside a read-only unit", datasource=manager.datasource
                )
        async with infrastructure_unit(manager, read_only=read_only) as session:
            unit = current_state().target(manager.datasource)
            connection = _DataConnection(session)
            try:
                await self._prepare_session(connection, namespace=namespace)
                yield connection
            except BaseException as error:
                # A rejected row must roll back earlier batch rows even when its caller catches the error.
                if not read_only and unit is not None:
                    unit.set_rollback_only(error)
                raise

    async def _verify_schema(self, conn: Any) -> None:
        """Raise unless the ``vector`` extension and the table exist; never creates anything."""
        has_extension = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        if not has_extension:
            raise VectorStoreError(
                "the 'vector' extension is not installed and this store was built with create_schema=False; "
                "install it in a migration (CREATE EXTENSION vector)"
            )
        table = await conn.fetchval("SELECT to_regclass($1)", self._table)
        if table is None:
            raise VectorStoreError(
                f"table {self._table!r} does not exist and this store was built with create_schema=False; "
                "create it in a migration"
            )

    async def _create_schema(self, conn: Any) -> None:
        """Create the extension, table, and indexes if absent. Idempotent.

        With ``create_schema=False`` this verifies instead (see :meth:`_verify_schema`).
        Subclasses may override to add deployment concerns (e.g. RLS policies)
        on the same connection via ``await super()._create_schema(conn)``.
        """
        if not self.create_schema:
            await self._verify_schema(conn)
            return
        # GUARDED, AND NOT BY ``IF NOT EXISTS`` ALONE.
        #
        # On a managed PostgreSQL the PERMISSION check runs BEFORE the existence check, so
        # ``CREATE EXTENSION IF NOT EXISTS vector`` raises against a database that already has it.
        # Azure Database for PostgreSQL:
        #
        #     Because vector isn't a trusted extension, only members of "azure_pg_admin" are
        #     allowed to use CREATE EXTENSION vector
        #
        # ``vector`` is untrusted in PostgreSQL's own sense, so a managed service is right to reserve
        # it to an administrator; the application role that opens this pool is not one and must not
        # be made one. Asking ``pg_extension`` first is the same question :meth:`_verify_schema`
        # already asks, so the two paths now agree about what "installed" means, and first use stops
        # failing on a database an administrator has already prepared.
        installed = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        if not installed:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id         TEXT PRIMARY KEY,
                namespace  TEXT NOT NULL DEFAULT 'default',
                embedding  vector({self._dimension}) NOT NULL,
                text       TEXT NOT NULL,
                metadata   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self._table}_hnsw
            ON {self._table} USING hnsw (embedding vector_cosine_ops)
            WITH (m = {self._hnsw_m}, ef_construction = {self._hnsw_ef_construction})
            """
        )
        await conn.execute(f"CREATE INDEX IF NOT EXISTS {self._table}_namespace ON {self._table} (namespace)")

    async def _prepare_session(self, conn: Any, *, namespace: str) -> None:
        """Per-transaction session-setup hook. Default: no-op.

        Override to run ``SET LOCAL`` statements on *conn* before the operation
        executes -- e.g. RLS GUCs, ``search_path``, or ``statement_timeout``.
        """
        return None

    # -- VectorStoreProtocol surface ---------------------------------------

    async def _upsert(self, documents: list[VectorDocument], namespace: str) -> None:
        rows = []
        for doc in documents:
            if doc.embedding is None:
                raise VectorStoreError(f"VectorDocument {doc.id!r} has no embedding; pgvector requires one.")
            rows.append((doc.id, namespace, _vector_literal(doc.embedding), doc.text, json.dumps(doc.metadata)))
        if not rows:
            return
        async with self._operation(namespace=namespace) as conn:
            written = await conn.fetchmany(
                f"""
                INSERT INTO {self._table} AS stored (id, namespace, embedding, text, metadata)
                VALUES ($1, $2, $3::vector, $4, $5::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    text      = EXCLUDED.text,
                    metadata  = EXCLUDED.metadata
                WHERE stored.namespace = EXCLUDED.namespace
                RETURNING 1
                """,
                rows,
            )
            if len(written) != len(rows):
                raise VectorStoreError("Upsert rejected: one or more document IDs are unavailable in this namespace.")

    async def _search(
        self,
        query_embedding: list[float],
        top_k: int,
        namespace: str,
        filters: list[SearchFilter] | None,
    ) -> list[SearchResult]:
        params: list[Any] = [_vector_literal(query_embedding), namespace, top_k]
        where = ["namespace = $2"]
        next_index = 4
        for f in filters or []:
            clause, clause_params, next_index = _filter_clause(f, next_index)
            where.append(clause)
            params.extend(clause_params)
        sql = f"""
            SELECT id, text, metadata, 1 - (embedding <=> $1::vector) AS score
            FROM {self._table}
            WHERE {" AND ".join(where)}
            ORDER BY embedding <=> $1::vector
            LIMIT $3
        """
        async with self._operation(namespace=namespace, read_only=True) as conn:
            await conn.execute(f"SET LOCAL hnsw.ef_search = {self._hnsw_ef_search}")
            records = await conn.fetch(sql, *params)
        return [
            SearchResult(
                document=VectorDocument(
                    id=str(rec["id"]),
                    text=rec["text"],
                    embedding=None,
                    metadata=_load_metadata(rec["metadata"]),
                    namespace=namespace,
                ),
                score=float(rec["score"]),
            )
            for rec in records
        ]

    async def _delete(self, ids: list[str], namespace: str) -> None:
        if not ids:
            return
        async with self._operation(namespace=namespace) as conn:
            await conn.execute(
                f"DELETE FROM {self._table} WHERE namespace = $1 AND id = ANY($2::text[])",
                namespace,
                [str(i) for i in ids],
            )


class _DataConnection:
    """Hook-compatible parameterized SQL through Data's guarded session, never a raw driver escape.

    Existing hooks keep ``execute(sql, *values)`` / ``fetchval`` / ``fetch`` and positional
    ``$n`` binds. Transaction, commit, close, and raw pooled connections are deliberately absent.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _execute(self, sql: str, args: tuple[Any, ...]) -> Any:
        from sqlalchemy import text

        statement = re.sub(r"\$(\d+)", lambda match: f":p_{match[1]} ", sql)
        return await self._session.execute(
            text(statement), {f"p_{index}": value for index, value in enumerate(args, 1)}
        )

    async def execute(self, sql: str, *args: Any) -> Any:
        return await self._execute(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return (await self._execute(sql, args)).scalar()

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list((await self._execute(sql, args)).mappings())

    async def fetchmany(self, sql: str, rows: list[tuple[Any, ...]]) -> list[Any]:
        # Sequential execution retains the original last-write-wins behavior for duplicate batch IDs.
        records = []
        for row in rows:
            records.extend(await self.fetch(sql, *row))
        return records


def _vector_literal(values: list[float]) -> str:
    """Render a vector as the pgvector text input literal ``[v1,v2,...]``."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _load_metadata(raw: Any) -> dict[str, Any]:
    """Decode a JSONB column (asyncpg returns it as ``str``) to a dict."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        return dict(json.loads(raw))
    return dict(raw)


def _filter_clause(f: SearchFilter, index: int) -> tuple[str, list[Any], int]:
    """Build a parameterised metadata predicate for one :class:`SearchFilter`.

    The metadata key and value are both bound as parameters (``metadata ->> $k``),
    so neither is interpolated into the SQL text. Returns the clause, its
    parameters, and the next free positional-parameter index.
    """
    if f.operator == "in":
        values = [str(v) for v in f.value]
        return f"metadata ->> ${index} = ANY(${index + 1}::text[])", [f.field, values], index + 2
    op = _SCALAR_OPERATORS.get(f.operator)
    if op is None:  # pragma: no cover - SearchFilter.operator is a constrained Literal
        raise VectorStoreError(f"Unsupported filter operator: {f.operator!r}")
    if f.operator == "ne":
        # IS DISTINCT FROM so NULL metadata keys are treated as "not equal".
        return f"metadata ->> ${index} IS DISTINCT FROM ${index + 1}", [f.field, str(f.value)], index + 2
    return f"metadata ->> ${index} {op} ${index + 1}", [f.field, str(f.value)], index + 2
