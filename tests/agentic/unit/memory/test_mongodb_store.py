"""Mongo constructor boundaries; real CRUD/session contracts run on a replica set.

See tests/integration/test_agentic_native_mongo_memory.py for full native database behavior.
"""

from __future__ import annotations

import pytest
from pymongo import AsyncMongoClient

from pyfly.agentic.memory.database_store import MongoDBStore
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager


@pytest.mark.parametrize("settings", [{"url": "mongodb://fictional.invalid"}, {"pool_size": 4}])
def test_standalone_connection_options_require_native_data_configuration(settings):
    with pytest.raises(ValueError, match="pyfly.data.document"):
        MongoDBStore(**settings)


def test_positional_url_cannot_create_an_independent_client():
    with pytest.raises(ValueError, match="pyfly.data.document"):
        MongoDBStore("mongodb://fictional.invalid")


def test_native_client_or_manager_is_required():
    with pytest.raises(ValueError, match="pyfly.data.document"):
        MongoDBStore()


def test_a_non_native_client_cannot_bypass_the_framework_driver_boundary():
    with pytest.raises(TypeError, match="AsyncMongoClient"):
        MongoDBStore(object())


@pytest.mark.parametrize("settings", [{"database": "bad/database"}, {"collection": "bad$name"}])
def test_document_identifiers_are_validated_before_connecting(settings):
    with pytest.raises(ValueError, match="names"):
        MongoDBStore(**settings)


async def test_injected_manager_must_serve_the_supplied_client():
    first = AsyncMongoClient("mongodb://fictional.invalid", connect=False)
    second = AsyncMongoClient("mongodb://fictional.invalid", connect=False)
    try:
        manager = MongoTransactionManager(first)
        with pytest.raises(ValueError, match="does not serve"):
            MongoDBStore(second, manager=manager)
        with pytest.raises(TypeError, match="MongoTransactionManager"):
            MongoDBStore(first, manager=object())
        store = MongoDBStore(manager=manager)
        assert store.database == "firefly_memory"
    finally:
        await first.close()
        await second.close()


async def test_existing_valid_collection_locations_are_preserved():
    client = AsyncMongoClient("mongodb://fictional.invalid", connect=False)
    try:
        store = MongoDBStore(client, database="existing-memory", collection="agent-facts.entries")
        assert store._collection.full_name == "existing-memory.agent-facts.entries"
    finally:
        await client.close()
