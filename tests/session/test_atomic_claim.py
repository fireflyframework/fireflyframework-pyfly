# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""One-shot session claims and revoked-response publication fences."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import Response

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.adapters.sql_session_store import SqlSessionStore
from pyfly.session.filter import SessionFilter
from tests.security.test_oauth2_callback_consumption import incoming


@pytest.fixture(params=["memory", "sql", "postgres", "redis"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield InMemorySessionStore()
    elif request.param == "redis":
        import redis.asyncio as aioredis

        from pyfly.session.adapters.redis import RedisSessionStore

        if not os.environ.get("PYFLY_IT_REDIS_URL"):
            pytest.skip("Owned Redis endpoint not configured")
        client = aioredis.from_url(os.environ["PYFLY_IT_REDIS_URL"])
        try:
            yield RedisSessionStore(client)
        finally:
            await client.aclose()
    else:
        if request.param == "postgres" and not os.environ.get("PYFLY_IT_POSTGRES_URL"):
            pytest.skip("Owned PostgreSQL endpoint not configured")
        url = (
            os.environ["PYFLY_IT_POSTGRES_URL"]
            if request.param == "postgres"
            else f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}"
        )
        engine = create_async_engine(url)
        instance = SqlSessionStore(engine, table="claim_" + uuid.uuid4().hex)
        await instance.start()
        try:
            yield instance
        finally:
            await engine.dispose()


async def test_claim_has_one_winner_preserves_current_attrs_and_fences_stale_save(store):
    await store.save("old", {"state": "challenge", "nested": {"empty": [], "n": 9007199254740993}}, 60)
    stale = dict(await store.get("old"))
    outcomes = await asyncio.gather(
        *(store.claim_attributes("old", f"new-{i}", {"state": "challenge"}, ("state",), 60) for i in range(2))
    )
    assert sum(value is not None for value in outcomes) == 1
    assert await store.get("old") is None
    assert not await store.replace("old", {**stale, "ordinary": True}, 60)
    winner = next(i for i, value in enumerate(outcomes) if value is not None)
    assert await store.get(f"new-{winner}") == {"nested": {"empty": [], "n": 9007199254740993}}
    assert outcomes[winner] == stale


async def test_mismatch_collision_and_expiry_leave_claim_unconsumed(store):
    await store.save("old", {"state": "CaseSensitive"}, 60)
    await store.save("occupied", {"other": True}, 60)
    assert await store.claim_attributes("old", "new", {"state": "casesensitive"}, ("state",), 60) is None
    assert await store.claim_attributes("old", "occupied", {"state": "CaseSensitive"}, ("state",), 60) is None
    assert await store.get("old") == {"state": "CaseSensitive"}
    await store.save("expired", {"state": "challenge"}, 1)
    await asyncio.sleep(1.05)
    assert await store.claim_attributes("expired", "new", {"state": "challenge"}, ("state",), 60) is None


@pytest.mark.parametrize("modified", [False, True])
async def test_late_loaded_request_never_reissues_revoked_cookie(store, modified):
    await store.save("old", {"authenticated": True}, 60)
    loaded, resume = asyncio.Event(), asyncio.Event()
    held: list[Any] = []

    async def read(request):
        held.append(request.state.session)
        loaded.set()
        await resume.wait()
        if modified:
            request.state.session.set_attribute("ordinary", True)
        return Response("profile")

    task = asyncio.create_task(SessionFilter(store).do_filter(incoming(cookie="old"), read))
    try:
        await asyncio.wait_for(loaded.wait(), 2)
        await store.delete("old")
        await store.save("new-login", {"authenticated": True}, 60)
        resume.set()
        response = await asyncio.wait_for(task, 2)
        assert "set-cookie" not in response.headers
        assert held[0].invalidated
        assert await store.get("old") is None
        assert await store.get("new-login") is not None
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_sql_malformed_session_does_not_log_bearer_identifier(tmp_path, caplog):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'malformed.db'}")
    store = SqlSessionStore(engine)
    canary = "canary-bearer-session-never-log"
    try:
        await store.save(canary, {}, 60)
        async with engine.begin() as connection:
            await connection.execute(update(store._table).values(data="{malformed"))
        assert await store.get(canary) is None
        assert canary not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "old_id,new_id,expected,names,ttl",
    [
        ("old", "old", {"state": "secret"}, ("state",), 60),
        ("old", "new", {}, ("state",), 60),
        ("old", "new", {"state": ""}, ("state",), 60),
        ("old", "new", {"state": "secret"}, ("other",), 60),
        ("old", "new", {"state": "secret"}, ("state", "state"), 60),
        ("old", "new", {"state": "secret"}, ("state",), 0),
    ],
)
async def test_invalid_claim_contract_never_writes(store, old_id, new_id, expected, names, ttl):
    await store.save("old", {"state": "secret"}, 60)
    with pytest.raises(ValueError, match="Invalid session claim parameters"):
        await store.claim_attributes(old_id, new_id, expected, names, ttl)
    assert await store.get("old") == {"state": "secret"}


async def test_sql_ambient_transaction_cannot_roll_back_a_successful_claim(store):
    if not isinstance(store, SqlSessionStore):
        pytest.skip("SQL transaction contract")
    from pyfly.data.transaction import TransactionTemplate
    from pyfly.session import SessionClaimUnavailable

    await store.save("old", {"state": "challenge", "ordinary": "before"}, 60)

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        async with TransactionTemplate(store.engine).transaction():
            await store.replace("old", {"state": "challenge", "ordinary": "not-committed"}, 60)
            with pytest.raises(SessionClaimUnavailable, match="ambient unit of work"):
                await store.claim_attributes("old", "new", {"state": "challenge"}, ("state",), 60)
            raise Rollback()
    assert await store.get("old") == {"state": "challenge", "ordinary": "before"}
    assert await store.get("new") is None


async def test_redis_malformed_session_and_unknown_type_do_not_log_bearer_values(caplog):
    from pyfly.session.adapters.redis import RedisSessionStore, _json_object_hook

    class Client:
        async def get(self, key):
            return b"{malformed"

    canary = "canary-bearer-session-never-log"
    assert await RedisSessionStore(Client()).get(canary) is None
    assert _json_object_hook({"__pyfly_type__": canary, "field": True}) == {"field": True}
    assert canary not in caplog.text
