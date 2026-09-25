# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Smoke tests proving the backend fixtures stand up a REAL backend and round-trip.

The broker and standalone-Mongo fixtures start one container each. The backend-matrix lanes are
opened one by one: every relational lane enforces foreign keys (sqlite-file included, which runs in
the fast suite), the MySQL/MariaDB lanes run with a working pool pre-ping, and the Mongo lane is a
replica set with a writable primary that commits and aborts transactions.

Run: PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration tests/integration/test_container_fixtures_smoke.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from pyfly.testing import requires_docker
from tests.support.backend_matrix import MARIADB, MYSQL, MongoBackend, RelationalBackend
from tests.support.contract_models import ContractChild, ContractParent

_EXPECTED_DIALECT = {"sqlite-file": "sqlite", "pg": "postgresql", "mysql": "mysql", "mariadb": "mariadb"}


async def test_relational_lane_opens_and_enforces_foreign_keys(relational_backend: RelationalBackend) -> None:
    """Every relational lane opens its own database and rejects a child whose parent does not exist."""
    await relational_backend.create_tables(ContractParent, ContractChild)
    engine = relational_backend.create_engine()
    assert engine.dialect.name == _EXPECTED_DIALECT[relational_backend.lane]
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        parent = ContractParent(name="parent")
        parent.children.append(ContractChild(label="kept", position=1))
        session.add(parent)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(ContractChild(parent_id=uuid.uuid4(), label="orphan", position=1))

    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM contract_child"))).scalar_one() == 1


@pytest.mark.backends(MYSQL, MARIADB)
async def test_mysql_lanes_replace_a_dead_pooled_connection_through_pre_ping(
    relational_backend: RelationalBackend,
) -> None:
    """The MySQL/MariaDB lanes run with pool pre-ping on, and it works: a pooled connection the
    server has killed is detected at checkout and replaced, instead of failing the next statement."""
    assert relational_backend.pre_ping
    engine = relational_backend.create_engine(pool_size=1, max_overflow=0)
    async with engine.connect() as conn:
        first_id = (await conn.execute(text("SELECT CONNECTION_ID()"))).scalar_one()

    killer = relational_backend.create_engine(poolclass=NullPool)
    async with killer.connect() as conn:
        await conn.execute(text(f"KILL {int(first_id)}"))

    for _ in range(5):  # every checkout, not just the first after the kill
        async with engine.connect() as conn:
            current_id = (await conn.execute(text("SELECT CONNECTION_ID()"))).scalar_one()
        assert current_id != first_id


async def test_mongo_replica_set_lane_commits_and_aborts_transactions(mongo_backend: MongoBackend) -> None:
    """The Mongo lane is a replica set with a writable primary: a transaction commits, and an aborted
    one leaves nothing behind (a standalone server would reject startTransaction)."""
    from pymongo import AsyncMongoClient

    client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(mongo_backend.url)
    try:
        hello = await client.admin.command("hello")
        assert hello["isWritablePrimary"] is True
        assert hello["setName"] == "rs0"

        collection = client[mongo_backend.database]["smoke"]
        await collection.insert_one({"k": "setup"})  # create the collection outside any transaction
        async with client.start_session() as session:
            async with await session.start_transaction():
                await collection.insert_one({"k": "committed"}, session=session)
            with pytest.raises(RuntimeError, match="abort"):
                async with await session.start_transaction():
                    await collection.insert_one({"k": "aborted"}, session=session)
                    raise RuntimeError("abort this transaction")

        stored = [doc["k"] async for doc in collection.find({}, {"_id": 0})]
        assert sorted(stored) == ["committed", "setup"]
    finally:
        await client.close()


@requires_docker
@pytest.mark.asyncio
async def test_mongo_fixture_roundtrips(mongo_url: str) -> None:
    motor = pytest.importorskip("motor.motor_asyncio")
    client = motor.AsyncIOMotorClient(mongo_url)
    try:
        coll = client["pyfly_it"]["smoke"]
        _id = await coll.insert_one({"k": "v"})
        doc = await coll.find_one({"_id": _id.inserted_id})
        assert doc is not None and doc["k"] == "v"
    finally:
        client.close()


@requires_docker
@pytest.mark.asyncio
async def test_kafka_fixture_roundtrips(kafka_url: str) -> None:
    aiokafka = pytest.importorskip("aiokafka")
    topic = f"pyfly-it-{uuid.uuid4().hex[:8]}"
    producer = aiokafka.AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()
    try:
        await producer.send_and_wait(topic, b"hello")
    finally:
        await producer.stop()

    consumer = aiokafka.AIOKafkaConsumer(
        topic,
        bootstrap_servers=kafka_url,
        auto_offset_reset="earliest",
        group_id="pyfly-it",
    )
    await consumer.start()
    try:
        msg = await asyncio.wait_for(consumer.getone(), timeout=10)  # fail fast instead of hanging
        assert msg.value == b"hello"
    finally:
        await consumer.stop()


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_fixture_roundtrips(amqp_url: str) -> None:
    aio_pika = pytest.importorskip("aio_pika")
    conn = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await conn.channel()
        queue = await channel.declare_queue(f"pyfly-it-{uuid.uuid4().hex[:8]}")
        await channel.default_exchange.publish(
            aio_pika.Message(body=b"ping"),
            routing_key=queue.name,
        )
        incoming = await queue.get(timeout=10)
        assert incoming is not None and incoming.body == b"ping"
        await incoming.ack()
        await channel.close()
    finally:
        await conn.close()
