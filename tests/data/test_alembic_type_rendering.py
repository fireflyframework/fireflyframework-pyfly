"""Portable revisions generated on SQLite must retain executable PostgreSQL JSON variants."""

from __future__ import annotations

import pytest
from alembic.autogenerate import render
from alembic.autogenerate.api import AutogenContext
from alembic.operations import Operations
from alembic.operations.ops import CreateTableOp
from alembic.runtime.migration import MigrationContext
from sqlalchemy import JSON, Column, Integer, MetaData, Table, Text, UnicodeText, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from pyfly.data.relational.sqlalchemy.types import render_item


@pytest.mark.parametrize("astext_type", [Text(), UnicodeText(collation="C")], ids=["text", "unicode-text"])
@pytest.mark.parametrize("none_as_null", [False, True])
def test_sqlite_generated_jsonb_variant_upgrades_and_keeps_postgres_options(astext_type, none_as_null):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table(
        "portable_json",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "payload",
            JSON().with_variant(postgresql.JSONB(none_as_null=none_as_null, astext_type=astext_type), "postgresql"),
        ),
    )
    try:
        with engine.begin() as connection:
            migration = MigrationContext.configure(connection)
            autogen = AutogenContext(
                migration,
                opts={
                    "sqlalchemy_module_prefix": "sa.",
                    "alembic_module_prefix": "op.",
                    "render_item": render_item,
                },
            )
            body = render.render_op_text(autogen, CreateTableOp.from_table(table))
            imports = "\n".join(["import sqlalchemy as sa", "from alembic import op", *sorted(autogen.imports)])
            namespace = {}
            with Operations.context(migration):
                exec(imports + "\ncreated_table = " + body, namespace)
            created = namespace["created_table"]
            assert inspect(connection).has_table("portable_json")
            variant = created.c.payload.type.dialect_impl(postgresql.dialect())
            assert isinstance(variant, postgresql.JSONB)
            assert variant.none_as_null is none_as_null
            assert type(variant.astext_type) is type(astext_type)
            assert variant.astext_type.collation == astext_type.collation
            assert "JSONB" in str(CreateTable(created).compile(dialect=postgresql.dialect()))
    finally:
        engine.dispose()
