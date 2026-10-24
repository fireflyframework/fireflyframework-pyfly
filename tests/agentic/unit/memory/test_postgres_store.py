"""Native PostgreSQL constructors preserve storage locations and reject unmanaged pools.

Real PostgreSQL CRUD/JSONB/TTL/transactions run in test_agentic_native_memory.py and pgvector tests.
"""

from __future__ import annotations

import pytest

from pyfly.agentic.memory.database_store import PostgreSQLStore
from pyfly.data.relational.framework_schema import framework_metadata


@pytest.mark.parametrize(
    "settings",
    [
        {"url": "postgresql://fictional.invalid/db"},
        {"pool_size": 4},
        {"pool_min_size": 1},
        {"timeout": 2},
    ],
)
def test_standalone_connection_options_require_native_data_configuration(settings):
    with pytest.raises(ValueError, match="pyfly.data.relational"):
        PostgreSQLStore(**settings)


def test_positional_url_cannot_create_an_independent_pool():
    with pytest.raises(ValueError, match="pyfly.data.relational"):
        PostgreSQLStore("postgresql://fictional.invalid/db")


@pytest.mark.parametrize("schema", ["bad; DROP SCHEMA public", "bad-name", "", "with space"])
def test_schema_identifiers_are_validated_before_data_access(schema):
    with pytest.raises(ValueError, match="schema"):
        PostgreSQLStore("primary", schema_name=schema)


def test_historical_and_custom_schema_locations_do_not_collide_in_metadata():
    historic = PostgreSQLStore("primary")
    custom = PostgreSQLStore("primary", schema_name="test_native_memory_schema")
    try:
        assert historic._table.fullname == "firefly_memory.memory_entries"
        assert custom._table.fullname == "test_native_memory_schema.memory_entries"
        assert historic._table is not custom._table
        assert {column.name for column in historic._table.primary_key} == {"namespace", "entry_id"}
    finally:
        framework_metadata.remove(historic._table)
        framework_metadata.remove(custom._table)
