"""Native Data ownership and migration contracts without a database."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyfly.agentic.vectorstores.pgvector_store import PgVectorVectorStore


class TestPgVectorVectorStoreUnit:
    @pytest.mark.parametrize(
        "legacy", [{"url": "postgresql://localhost/db"}, {"pool_min_size": 1}, {"pool_max_size": 3}]
    )
    def test_connection_options_require_data_migration(self, legacy: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="pyfly.data.relational"):
            PgVectorVectorStore(dimension=8, **legacy)

    def test_positional_url_requires_data_migration(self) -> None:
        with pytest.raises(ValueError, match="pyfly.data.relational"):
            PgVectorVectorStore("postgresql://localhost/db", dimension=8)

    def test_constructor_params(self) -> None:
        store = PgVectorVectorStore(
            object(),
            dimension=1536,
            table_name="my_vectors",
            hnsw_m=32,
            hnsw_ef_construction=128,
            hnsw_ef_search=300,
        )
        assert (store._dimension, store._table) == (1536, "my_vectors")
        assert (store._hnsw_m, store._hnsw_ef_construction, store._hnsw_ef_search) == (32, 128, 300)

    @pytest.mark.parametrize(
        "options",
        [{"dimension": 0}, {"dimension": -1}, {"dimension": 8, "hnsw_m": 0}, {"dimension": 8, "hnsw_ef_search": 0}],
    )
    def test_invalid_vector_parameters_rejected(self, options: dict[str, int]) -> None:
        with pytest.raises(ValueError):
            PgVectorVectorStore(object(), **options)

    def test_invalid_table_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="table_name"):
            PgVectorVectorStore(object(), dimension=8, table_name="bad name; DROP")

    async def test_prepare_session_default_is_noop(self) -> None:
        store = PgVectorVectorStore(object(), dimension=8)
        assert await store._prepare_session(object(), namespace="anything") is None

    @pytest.mark.parametrize(
        "policy,expected", [("create", True), ("create-drop", True), ("none", False), ("validate", False)]
    )
    def test_default_creation_uses_datasource_policy(self, policy: str, expected: bool) -> None:
        target = SimpleNamespace(properties=SimpleNamespace(ddl_auto=policy))
        assert PgVectorVectorStore(target, dimension=8).create_schema is expected

    async def test_schema_create_or_verify_preserves_extension_permissions(self) -> None:
        from pyfly.agentic.exceptions import VectorStoreError

        class FakeConn:
            def __init__(self, extension: bool, table: bool = True) -> None:
                self.extension = extension
                self.table = table
                self.statements: list[str] = []

            async def execute(self, sql: str, *args: object) -> None:
                self.statements.append(sql)

            async def fetchval(self, sql: str, *args: object) -> object:
                self.statements.append(sql)
                return self.extension if "pg_extension" in sql else ("vector_documents" if self.table else None)

        validate = PgVectorVectorStore(object(), dimension=8, create_schema=False)
        ok = FakeConn(True)
        await validate._create_schema(ok)
        assert not any(sql.lstrip().startswith("CREATE") for sql in ok.statements)
        with pytest.raises(VectorStoreError, match="extension"):
            await validate._create_schema(FakeConn(False))
        with pytest.raises(VectorStoreError, match="vector_documents"):
            await validate._create_schema(FakeConn(True, False))
        create = PgVectorVectorStore(object(), dimension=8, create_schema=True)
        installed = FakeConn(True)
        await create._create_schema(installed)
        assert not any("CREATE EXTENSION" in sql for sql in installed.statements)
        assert any("CREATE TABLE" in sql for sql in installed.statements)
        missing = FakeConn(False)
        await create._create_schema(missing)
        assert any("CREATE EXTENSION" in sql for sql in missing.statements)
