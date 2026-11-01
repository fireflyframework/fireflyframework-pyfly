# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL coordination shared by independent cache instances."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from pyfly.cache.adapters.postgres import PostgresCacheAdapter
from pyfly.data.transaction import TransactionTemplate
from tests.support.backend_matrix import PG, RelationalBackend

pytestmark = pytest.mark.backends(PG)


def test_coordination_is_a_separate_optional_port() -> None:
    from pyfly.cache import CacheCoordination
    from pyfly.cache.adapters.memory import InMemoryCache

    assert not isinstance(InMemoryCache(), CacheCoordination)


async def _nodes(backend: RelationalBackend) -> tuple[PostgresCacheAdapter, PostgresCacheAdapter]:
    nodes = tuple(PostgresCacheAdapter(backend.create_engine(), purge_interval=None) for _ in range(2))
    for cache in nodes:
        await cache.start()
    return nodes  # type: ignore[return-value]


async def test_compare_and_set_matches_present_none_and_not_absence(relational_backend: RelationalBackend) -> None:
    from pyfly.cache import CacheCoordination

    first, second = await _nodes(relational_backend)
    assert isinstance(first, CacheCoordination)
    assert not await first.compare_and_set("missing", None, "new")
    await first.put("present", None)
    assert await second.compare_and_set("present", None, {"revision": 1})
    outcomes = await asyncio.gather(
        *(
            node.compare_and_set("present", {"revision": 1}, {"revision": n})
            for n, node in enumerate((first, second), 2)
        )
    )
    assert outcomes.count(True) == 1
    assert not await first.evict_if_value("present", {"revision": 1})
    assert await second.evict_if_value("present", await first.get("present"))


async def test_stale_owner_cannot_release_or_replace_new_owner(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    assert await first.put_if_absent("owner", "a", timedelta(milliseconds=30))
    await asyncio.sleep(0.06)
    assert not await first.compare_and_set("owner", "a", "expired")
    assert not await first.evict_if_value("owner", "a")
    assert await second.put_if_absent("owner", "b", timedelta(seconds=10))
    assert not await first.evict_if_value("owner", "a")
    assert not await first.compare_and_set("owner", "a", "stale")
    assert await second.get("owner") == "b"


async def test_rolling_window_race_has_one_shared_limit(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    outcomes = await asyncio.gather(
        *(node.consume_window("actor", 5, timedelta(seconds=30)) for node in (first, second) for _ in range(12))
    )
    assert sum(outcomes) == 5
    assert not await first.consume_window("actor", 5, timedelta(seconds=30))
    assert await first.with_namespace("another").consume_window("actor", 1, timedelta(seconds=30))
    await first.clear()
    assert not await second.consume_window("actor", 5, timedelta(seconds=30))


async def test_window_is_rolling_and_denial_never_extends_it(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    window = timedelta(milliseconds=600)
    assert await first.consume_window("rolling", 3, window)
    assert await second.consume_window("rolling", 3, window)
    await asyncio.sleep(0.4)
    assert await first.consume_window("rolling", 3, window)
    assert not await second.consume_window("rolling", 3, window)
    await asyncio.sleep(0.25)
    assert await second.consume_window("rolling", 3, window)
    assert await first.consume_window("rolling", 3, window)
    assert not await first.consume_window("rolling", 3, window)


async def test_coordination_commits_independently_of_business_rollback(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    await first.put("owner", "a")
    with pytest.raises(RuntimeError, match="business rollback"):
        async with TransactionTemplate(first.engine).transaction():
            assert await first.compare_and_set("owner", "a", "b", timedelta(seconds=30))
            assert await first.consume_window("rate", 1, timedelta(seconds=30))
            raise RuntimeError("business rollback")
    assert await first.get("owner") == "b"
    assert not await first.consume_window("rate", 1, timedelta(seconds=30))


async def test_clear_window_is_per_key_and_serializes_with_admission(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    window = timedelta(seconds=30)
    assert await first.consume_window("actor", 1, window)
    assert await first.consume_window("other", 1, window)
    assert await second.clear_window("actor")
    assert not await first.clear_window("missing")
    assert await second.consume_window("actor", 1, window)
    await asyncio.gather(
        *(
            operation
            for _ in range(10)
            for operation in (first.clear_window("actor"), second.consume_window("actor", 1, window))
        )
    )
    assert not await first.consume_window("other", 1, window)
    await first.clear_window("actor")
    outcomes = await asyncio.gather(*(node.consume_window("actor", 1, window) for node in (first, second)))
    assert sum(outcomes) == 1


async def test_invalid_policy_and_changed_live_window_fail_closed(relational_backend: RelationalBackend) -> None:
    first, _ = await _nodes(relational_backend)
    for limit, window in [
        (0, timedelta(seconds=1)),
        (True, timedelta(seconds=1)),
        (10001, timedelta(seconds=1)),
        (1, timedelta(0)),
    ]:
        with pytest.raises(ValueError):
            await first.consume_window("invalid", limit, window)
    assert await first.consume_window("policy", 2, timedelta(seconds=30))
    with pytest.raises(ValueError, match="window"):
        await first.consume_window("policy", 2, timedelta(seconds=60))


async def test_purge_is_bounded_and_cannot_remove_live_coordination(relational_backend: RelationalBackend) -> None:
    from pyfly.data.relational.framework_schema import cache_windows

    first, second = await _nodes(relational_backend)
    first.PURGE_BATCH = 2
    for key in ("a", "b", "c"):
        assert await first.consume_window(key, 1, timedelta(milliseconds=20))
    assert await second.consume_window("live", 1, timedelta(seconds=30))
    await asyncio.sleep(0.05)
    assert await first.purge_expired_windows() == 2
    assert await first.purge_expired_windows() == 1
    assert not await second.consume_window("live", 1, timedelta(seconds=30))
    async with first.engine.connect() as connection:
        assert (await connection.execute(select(func.count()).select_from(cache_windows))).scalar_one() == 1


async def test_admission_uses_database_time_not_injected_node_clock(relational_backend: RelationalBackend) -> None:
    first, second = await _nodes(relational_backend)
    async with first.engine.connect() as connection:
        now = (await connection.execute(text("SELECT clock_timestamp()"))).scalar_one()
    first._clock = lambda: now - timedelta(days=100)
    second._clock = lambda: now + timedelta(days=100)
    assert await first.consume_window("clock", 1, timedelta(seconds=30))
    assert not await second.consume_window("clock", 1, timedelta(seconds=30))


async def test_replica_caller_keeps_its_unit_while_coordination_uses_primary(
    relational_backend: RelationalBackend,
) -> None:
    from sqlalchemy import event

    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.transaction import current_unit_of_work, outside_transaction

    config = Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "url": relational_backend.url,
                        "read-replica": {"url": relational_backend.url},
                        "ddl-auto": "create",
                    }
                }
            }
        }
    )
    registry = DataSourceRegistry(config)
    primary = registry.primary
    assert primary.replica is not None
    observed: list[str] = []

    def replica_query(*args) -> None:
        if "pyfly_cache_" in args[2]:
            observed.append(args[2])

    event.listen(primary.replica.engine.sync_engine, "before_cursor_execute", replica_query)
    cache = PostgresCacheAdapter(primary)
    try:
        await cache.start()
        await cache.put("owner", "a")
        async with TransactionTemplate(primary, read_only=True).transaction() as caller:
            assert caller is not None and not caller.read_from_primary
            assert await cache.consume_window("actor", 1, timedelta(seconds=30))
            assert await cache.compare_and_set("owner", "a", "b")
            with outside_transaction():
                assert await cache.get("owner") == "b"
            assert current_unit_of_work(primary.name) is caller
        assert observed == []
        bad = PostgresCacheAdapter(primary.replica)
        with pytest.raises(ValueError, match="replica"):
            await bad.consume_window("actor", 1, timedelta(seconds=30))
    finally:
        await registry.close()


async def test_typed_decorator_hits_and_evictions_cross_instances(relational_backend: RelationalBackend) -> None:
    from pyfly.cache import cache_evict, cacheable

    first, second = await _nodes(relational_backend)
    calls: list[str] = []

    def reader(node: PostgresCacheAdapter):
        @cacheable(backend=node, key="typed:{key}", ttl=timedelta(seconds=30))
        async def read(key: str) -> tuple[int, str]:
            calls.append(key)
            return (42, key)

        return read

    a, b = reader(first), reader(second)
    assert await a("item") == (42, "item")
    assert await b("item") == (42, "item")
    assert calls == ["item"]

    @cache_evict(backend=first, key="typed:{key}")
    async def change(key: str) -> None:
        pass

    await change("item")
    assert await b("item") == (42, "item")
    assert calls == ["item", "item"]


async def test_cancellation_while_waiting_does_not_consume(relational_backend: RelationalBackend) -> None:
    from pyfly.data.relational.framework_schema import cache_windows

    first, second = await _nodes(relational_backend)
    window = timedelta(seconds=30)
    assert await first.consume_window("locked", 2, window)
    async with first.engine.begin() as connection:
        await connection.execute(
            select(cache_windows).where(cache_windows.c.cache_key == first.namespace + "locked").with_for_update()
        )
        pending = asyncio.create_task(second.consume_window("locked", 2, window))
        await asyncio.sleep(0.03)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert await first.consume_window("locked", 2, window)
    assert not await second.consume_window("locked", 2, window)


@pytest.mark.parametrize("operation", ["compare_and_set", "evict_if_value"])
async def test_conditional_value_expiry_is_rechecked_after_lock_wait(
    relational_backend: RelationalBackend,
    operation: str,
) -> None:
    from pyfly.data.relational.framework_schema import cache_entries

    first, second = await _nodes(relational_backend)
    await first.put("expiring", "owner", timedelta(milliseconds=150))
    async with first.engine.begin() as connection:
        await connection.execute(
            select(cache_entries).where(cache_entries.c.cache_key == first.namespace + "expiring").with_for_update()
        )
        if operation == "compare_and_set":
            action = second.compare_and_set("expiring", "owner", "new", timedelta(seconds=30))
        else:
            action = second.evict_if_value("expiring", "owner")
        pending = asyncio.create_task(action)
        await asyncio.sleep(0.25)
        assert not pending.done()
    assert not await pending
    assert await first.get("expiring") is None


async def test_cache_purge_cannot_delete_a_concurrently_renewed_owner(relational_backend: RelationalBackend) -> None:
    from sqlalchemy import update

    from pyfly.cache.serialization import cache_dumps
    from pyfly.data.relational.framework_schema import cache_entries

    first, second = await _nodes(relational_backend)
    await first.put("renewed", "old", timedelta(seconds=-1))
    async with first.engine.begin() as connection:
        await connection.execute(
            update(cache_entries)
            .where(cache_entries.c.cache_key == first.namespace + "renewed")
            .values(value=cache_dumps("new"), expires_at=func.clock_timestamp() + timedelta(hours=1))
        )
        pending = asyncio.create_task(second._purge_batch())
        await asyncio.sleep(0.05)
    assert await pending == 0
    assert await first.get("renewed") == "new"


async def test_default_cache_ttl_uses_db_time_despite_opposite_node_clock_skew(
    relational_backend: RelationalBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from pyfly.cache.adapters import postgres

    first, second = await _nodes(relational_backend)
    real_now = datetime.now(UTC)

    class SkewedClock(datetime):
        offset = timedelta(days=-100)

        @classmethod
        def now(cls, tz=None):
            return real_now + cls.offset

    monkeypatch.setattr(postgres, "datetime", SkewedClock)
    assert await first.put_if_absent("skew", "a", timedelta(milliseconds=150))
    SkewedClock.offset = timedelta(days=100)
    assert await second.get("skew") == "a"
    assert not await second.put_if_absent("skew", "b", timedelta(seconds=30))
    await second.put("live", "kept", timedelta(seconds=30))
    assert await first.purge_expired() == 0
    await asyncio.sleep(0.2)
    assert await first.get("skew") is None
    assert await second.put_if_absent("skew", "b", timedelta(seconds=30))
    assert await first.get("live") == "kept"


async def test_explicit_cache_clock_remains_an_opt_in_override(relational_backend: RelationalBackend) -> None:
    from datetime import UTC, datetime

    instant = datetime(2026, 1, 1, tzinfo=UTC)
    cache = PostgresCacheAdapter(relational_backend.create_engine(), clock=lambda: instant, purge_interval=None)
    await cache.start()
    child = cache.with_namespace("explicit-clock")
    await child.put("key", "kept", timedelta(seconds=30))
    assert await child.get("key") == "kept"
    instant += timedelta(seconds=31)
    assert await child.get("key") is None
    assert await child.purge_expired() == 1


@pytest.mark.parametrize("month, day", [(3, 8), (11, 1)])
@pytest.mark.parametrize("operation", ["put", "put_if_absent"])
async def test_database_ttl_is_elapsed_seconds_across_session_timezone_dst(
    relational_backend: RelationalBackend,
    monkeypatch: pytest.MonkeyPatch,
    month: int,
    day: int,
    operation: str,
) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import DateTime, literal

    from pyfly.data.relational.framework_schema import cache_entries

    engine = relational_backend.create_engine(connect_args={"server_settings": {"timezone": "America/Los_Angeles"}})
    cache = PostgresCacheAdapter(engine, purge_interval=None)
    await cache.start()
    instant = datetime(2026, month, day, 4, tzinfo=UTC)
    monkeypatch.setattr(cache, "_now", lambda: literal(instant, type_=DateTime(timezone=True)))
    await getattr(cache, operation)("dst", "value", timedelta(days=1))
    async with engine.connect() as connection:
        expiry = (
            await connection.execute(
                select(cache_entries.c.expires_at).where(cache_entries.c.cache_key == cache.namespace + "dst")
            )
        ).scalar_one()
    assert expiry == instant + timedelta(seconds=86400)


@pytest.mark.parametrize("month, day", [(3, 8), (11, 1)])
@pytest.mark.parametrize("operation", ["compare_and_set", "consume_window"])
async def test_psycopg_coordination_uses_utc_elapsed_time(
    relational_backend: RelationalBackend,
    month: int,
    day: int,
    operation: str,
) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import event

    from pyfly.data.relational.framework_schema import cache_entries, cache_windows

    pytest.importorskip("psycopg")
    engine = relational_backend.with_driver("psycopg").create_engine(
        connect_args={"options": "-c timezone=America/Los_Angeles"}
    )
    instant = datetime(2026, month, day, 4, tzinfo=UTC)
    current = instant

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def fixed_database_clock(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT clock_timestamp()"):
            return f"SELECT TIMESTAMPTZ '{current.isoformat()}'", parameters
        return statement, parameters

    cache = PostgresCacheAdapter(engine, purge_interval=None)
    await cache.start()
    if operation == "compare_and_set":
        await cache.put("dst-owner", "a")
        assert await cache.compare_and_set("dst-owner", "a", "b", timedelta(days=1))
        table = cache_entries
    else:
        assert await cache.consume_window("dst-owner", 1, timedelta(days=1))
        table = cache_windows
    async with engine.connect() as connection:
        expiry = (
            await connection.execute(
                select(table.c.expires_at).where(table.c.cache_key == cache.namespace + "dst-owner")
            )
        ).scalar_one()
    assert expiry == instant + timedelta(seconds=86400)
    if operation == "consume_window":
        current = instant + timedelta(seconds=86399)
        assert not await cache.consume_window("dst-owner", 1, timedelta(days=1))
        current = instant + timedelta(seconds=86400)
        assert await cache.consume_window("dst-owner", 1, timedelta(days=1))
