# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""PostgreSQL cache adapter's atomic operations over primary units and database time."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from pyfly.cache.serialization import cache_dumps

if TYPE_CHECKING:
    from pyfly.cache.adapters.postgres import PostgresCacheAdapter


def _key(cache: PostgresCacheAdapter, key: str) -> str:
    stored = cache._key(key)
    cache._check_key(stored)
    return stored


async def compare_and_set(
    cache: PostgresCacheAdapter, key: str, expected: Any, value: Any, ttl: timedelta | None
) -> bool:
    stored, old, new = _key(cache, key), cache_dumps(expected), cache_dumps(value)
    table = cache._table
    async with cache._coordination_unit() as session:
        now = await _locked_match(cache, session, stored, old)
        if now is None:
            return False
        await session.execute(
            update(table)
            .where(table.c.cache_key == stored)
            .values(
                value=new,
                expires_at=now + ttl if ttl is not None else None,
            )
        )
    return True


async def evict_if_value(cache: PostgresCacheAdapter, key: str, expected: Any) -> bool:
    stored, value = _key(cache, key), cache_dumps(expected)
    table = cache._table
    async with cache._coordination_unit() as session:
        if await _locked_match(cache, session, stored, value) is None:
            return False
        await session.execute(delete(table).where(table.c.cache_key == stored))
    return True


async def _locked_match(cache: PostgresCacheAdapter, session: Any, key: str, expected: bytes) -> datetime | None:
    table = cache._table
    row = (
        await session.execute(
            select(table.c.value, table.c.expires_at).where(table.c.cache_key == key).with_for_update()
        )
    ).first()
    if row is None:
        return None
    # A predicate in UPDATE/DELETE may be evaluated before waiting on an unchanged row lock. The
    # explicit lock must precede the clock read, or an owner could act after its lease expired.
    now: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    if bytes(row.value) != expected or (row.expires_at is not None and row.expires_at <= now):
        return None
    return now


async def consume_window(cache: PostgresCacheAdapter, key: str, limit: int, window: timedelta) -> bool:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("CacheCoordination limit must be an integer between 1 and 10000")
    if not isinstance(window, timedelta) or not timedelta(0) < window <= timedelta(days=365):
        raise ValueError("CacheCoordination window must be positive and no longer than 365 days")
    stored = _key(cache, key)
    micros = (window.days * 86400 + window.seconds) * 1_000_000 + window.microseconds
    table = cache._window_table
    # The no-op conflict update locks and returns the row atomically. A separate INSERT/SELECT pair
    # would race clear_window or cleanup deleting it between statements.
    statement = insert(table).values(
        cache_key=stored,
        events=[],
        expires_at=func.clock_timestamp(),
        window_microseconds=micros,
    )
    locked_statement = statement.on_conflict_do_update(
        index_elements=[table.c.cache_key],
        set_={"cache_key": statement.excluded.cache_key},
    ).returning(table.c.events, table.c.expires_at, table.c.window_microseconds)
    async with cache._coordination_unit() as session:
        row = (await session.execute(locked_statement)).one()
        # Read after acquiring the row lock: time spent waiting is not charged to the caller's window.
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if row.expires_at > now and row.window_microseconds != micros:
            raise ValueError("Changing a live cache window requires a policy-versioned key")
        events = [datetime.fromisoformat(value) for value in row.events] if row.expires_at > now else []
        if len(events) > 10000 or any(value.tzinfo is None or value > now for value in events):
            raise ValueError("Invalid cache window state")
        live = [value for value in events if value > now - window]
        admitted = len(live) < limit
        if admitted:
            live.append(now)
        await session.execute(
            update(table)
            .where(table.c.cache_key == stored)
            .values(
                events=[value.isoformat() for value in live],
                expires_at=(live[-1] + window) if live else now,
                window_microseconds=micros,
            )
        )
    return admitted


async def clear_window(cache: PostgresCacheAdapter, key: str) -> bool:
    table = cache._window_table
    statement = delete(table).where(table.c.cache_key == _key(cache, key)).returning(table.c.cache_key)
    async with cache._coordination_unit() as session:
        return (await session.execute(statement)).scalar_one_or_none() is not None


async def purge_expired_windows(cache: PostgresCacheAdapter) -> int:
    table = cache._window_table
    expired = table.c.expires_at <= func.clock_timestamp()
    batch = select(table.c.cache_key).where(expired).limit(cache.PURGE_BATCH).with_for_update(skip_locked=True)
    statement = delete(table).where(table.c.cache_key.in_(batch), expired).returning(table.c.cache_key)
    async with cache._coordination_unit() as session:
        return len((await session.execute(statement)).all())
