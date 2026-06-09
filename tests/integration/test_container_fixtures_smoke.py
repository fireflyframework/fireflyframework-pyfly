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
"""Smoke tests proving the SP-1 backend fixtures stand up a REAL container and round-trip.

Run: PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration tests/integration/test_container_fixtures_smoke.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pyfly.testing import requires_docker


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
