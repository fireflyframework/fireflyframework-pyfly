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
"""Entity timestamps are one UTC instant with microseconds on every relational lane (C047, C124, C125, C178).

``BaseEntity.created_at``/``updated_at`` and ``SoftDeleteMixin.deleted_at`` used to be a plain
``DateTime(timezone=True)``: aware on PostgreSQL, naive wall time with the offset dropped on SQLite,
MySQL and MariaDB, host-local for a naive value on PostgreSQL, and whole seconds on MySQL/MariaDB (MySQL
rounding into the future). They are :class:`~pyfly.data.relational.sqlalchemy.types.UtcDateTime` now, and
so is an application column that declares it. Every test reloads from the database: the in-memory
default never proved anything. The sqlite-file lane runs in the fast suite, the server lanes in the
integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.types import UtcDateTime
from tests.support.backend_matrix import RelationalBackend

PLUS_TWO = timezone(timedelta(hours=2))


class TimestampedEvent(SoftDeleteMixin, BaseEntity):
    """The framework's timestamp columns plus an application column typed ``UtcDateTime``."""

    __tablename__ = "wp05_timestamped_event"

    label: Mapped[str] = mapped_column(String(50))
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)


class TimestampedEventRepository(Repository[TimestampedEvent, Any]):
    async def find_by_due_at_between(self, low: datetime, high: datetime) -> list[TimestampedEvent]: ...
    async def find_by_due_at_less_than(self, bound: datetime) -> list[TimestampedEvent]: ...
    async def find_by_created_at_greater_than(self, bound: datetime) -> list[TimestampedEvent]: ...


async def _sessions(backend: RelationalBackend) -> async_sessionmaker[AsyncSession]:
    await backend.create_tables(TimestampedEvent)
    return async_sessionmaker(backend.create_engine(), expire_on_commit=False)


def _repository(session: AsyncSession) -> TimestampedEventRepository:
    repository = TimestampedEventRepository(session=session)
    RepositoryBeanPostProcessor().after_init(repository, "timestampedEventRepository")
    return repository


def _is_utc(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() == timedelta(0)


async def _reload(factory: async_sessionmaker[AsyncSession], event: TimestampedEvent) -> TimestampedEvent:
    async with factory() as session:
        found = await session.get(TimestampedEvent, event.id, execution_options={"include_deleted": True})
    assert found is not None
    return found


async def test_framework_timestamps_reload_as_aware_utc_with_microseconds(
    relational_backend: RelationalBackend,
) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session:
        event = TimestampedEvent(label="stamped")
        session.add(event)
        await session.flush()
        stamped = event.created_at  # the Python default: aware UTC with microseconds
        await session.commit()

    reloaded = await _reload(factory, event)

    assert _is_utc(reloaded.created_at)
    assert _is_utc(reloaded.updated_at)
    # The exact instant, microseconds included: MySQL used to round it into the future, MariaDB to truncate.
    assert reloaded.created_at == stamped
    assert reloaded.created_at.microsecond == stamped.microsecond


async def test_save_returns_aware_utc_after_its_refresh(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session:
        saved = await _repository(session).save(TimestampedEvent(label="saved"))
        await session.commit()

    assert _is_utc(saved.created_at)
    assert saved.created_at <= datetime.now(UTC)  # a TypeError (naive vs aware) on three backends before


async def test_an_offset_value_is_stored_as_its_utc_instant(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    written = datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=PLUS_TWO)
    async with factory() as session, session.begin():
        event = TimestampedEvent(label="madrid", due_at=written)
        session.add(event)

    reloaded = await _reload(factory, event)

    assert _is_utc(reloaded.due_at)
    assert reloaded.due_at == datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=UTC)


async def test_a_naive_value_is_taken_as_utc(relational_backend: RelationalBackend) -> None:
    """PostgreSQL (asyncpg) read a naive value as the host's local time: the stored instant drifted by the
    host's UTC offset. Every lane now stores it as UTC."""
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        event = TimestampedEvent(label="naive", due_at=datetime(2026, 9, 24, 10, 0))
        session.add(event)

    reloaded = await _reload(factory, event)

    assert reloaded.due_at == datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


async def test_derived_queries_compare_instants_whatever_the_parameter_offset(
    relational_backend: RelationalBackend,
) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session:
        repository = _repository(session)
        await repository.save(TimestampedEvent(label="due", due_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC)))
        await session.commit()

        # 11:30+02:00 is 09:30Z and 12:30+02:00 is 10:30Z: the row at 10:00Z is inside.
        low = datetime(2026, 9, 24, 11, 30, tzinfo=PLUS_TWO)
        high = datetime(2026, 9, 24, 12, 30, tzinfo=PLUS_TWO)
        assert [e.label for e in await repository.find_by_due_at_between(low, high)] == ["due"]
        # 13:00+02:00 is 11:00Z, after the row.
        assert len(await repository.find_by_due_at_less_than(datetime(2026, 9, 24, 13, 0, tzinfo=PLUS_TWO))) == 1
        # Ten minutes ago, written in +02:00: the row was created after it.
        since = (datetime.now(UTC) - timedelta(minutes=10)).astimezone(PLUS_TWO)
        assert len(await repository.find_by_created_at_greater_than(since)) == 1


async def test_same_instance_arithmetic_after_an_update(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        event = TimestampedEvent(label="v1")
        session.add(event)

    async with factory() as session, session.begin():
        loaded = await session.get(TimestampedEvent, event.id)
        assert loaded is not None
        loaded.label = "v2"
        await session.flush()
        # updated_at comes from the flush, created_at from the database: both are aware UTC.
        elapsed = loaded.updated_at - loaded.created_at
    assert elapsed >= timedelta(0)


async def test_a_purge_cutoff_with_an_offset_skips_rows_deleted_seconds_ago(
    relational_backend: RelationalBackend,
) -> None:
    """``deleted_at < now - 1 h`` written in +02:00 matched a row deleted seconds earlier on SQLite, MySQL
    and MariaDB, so a retention job would hard-delete it."""
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        session.add(TimestampedEvent(label="just deleted", deleted_at=datetime.now(UTC)))

    cutoff = (datetime.now(UTC) - timedelta(hours=1)).astimezone(PLUS_TWO)
    async with factory() as session:
        stmt = select(TimestampedEvent).where(TimestampedEvent.deleted_at < cutoff)
        rows = (await session.execute(stmt.execution_options(include_deleted=True))).scalars().all()
    assert rows == []


async def test_two_instants_milliseconds_apart_stay_distinct_and_ordered(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    first = datetime(2026, 9, 24, 10, 0, 0, 700000, tzinfo=UTC)
    second = first + timedelta(milliseconds=3)
    async with factory() as session, session.begin():
        session.add_all([TimestampedEvent(label="p1", due_at=first), TimestampedEvent(label="p2", due_at=second)])

    async with factory() as session:
        after_first = (
            (await session.execute(select(TimestampedEvent.label).where(TimestampedEvent.due_at > first)))
            .scalars()
            .all()
        )
    assert after_first == ["p2"]


@pytest.mark.backends("pg")
async def test_asyncpg_reads_aware_utc_whatever_the_server_time_zone(relational_backend: RelationalBackend) -> None:
    """``UtcDateTime`` skips its result processing on asyncpg: the driver returns aware UTC even when the
    session's ``TimeZone`` is not UTC, so reads stay at native speed and still come back in UTC."""
    await relational_backend.create_tables(TimestampedEvent)
    engine = relational_backend.create_engine(connect_args={"server_settings": {"timezone": "Asia/Tokyo"}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    written = datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=PLUS_TWO)
    async with factory() as session, session.begin():
        event = TimestampedEvent(label="tokyo", due_at=written)
        session.add(event)

    async with factory() as session:
        assert (await session.execute(text("SELECT current_setting('TimeZone')"))).scalar_one() == "Asia/Tokyo"
    reloaded = await _reload(factory, event)

    assert _is_utc(reloaded.due_at)
    assert reloaded.due_at == written


@pytest.mark.backends("mysql", "mariadb")
async def test_mysql_family_columns_are_datetime_6(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(TimestampedEvent)
    engine = relational_backend.create_engine()
    table = TimestampedEvent.__tablename__
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {column["name"]: column["type"] for column in inspect(sync).get_columns(table)}
        )
    for name in ("created_at", "updated_at", "deleted_at", "due_at"):
        assert getattr(columns[name], "fsp", None) == 6, (name, columns[name])


@pytest.mark.backends("mariadb")
async def test_mariadb_through_a_mysql_url_keeps_microseconds(relational_backend: RelationalBackend) -> None:
    """``mysql+asyncmy://`` against MariaDB (dialect ``mysql``) gets ``DATETIME(6)`` too."""
    url = make_url(relational_backend.url).set(drivername="mysql+asyncmy")
    backend = RelationalBackend(relational_backend.lane, url.render_as_string(hide_password=False))
    factory = await _sessions(backend)
    written = datetime(2026, 9, 24, 12, 0, 0, 654321, tzinfo=PLUS_TWO)
    try:
        async with factory() as session, session.begin():
            event = TimestampedEvent(label="mysql-url", due_at=written)
            session.add(event)
        reloaded = await _reload(factory, event)
        assert reloaded.due_at == written and _is_utc(reloaded.due_at)
    finally:
        await backend.dispose()


@pytest.mark.backends("pg")
async def test_a_legacy_naive_postgresql_column_is_migrated_before_it_adopts_the_type(
    relational_backend: RelationalBackend,
) -> None:
    """``UtcDateTime`` expects ``timestamptz`` on PostgreSQL. A ``timestamp without time zone`` column that
    adopts it (``update_type_annotation_map`` makes every ``Mapped[datetime]`` one) reads back naive on
    asyncpg, and the server converts its writes with the session's ``TimeZone``. The documented migration
    reads the old values as the UTC wall times they are."""
    table = Table("wp05_legacy_naive", MetaData(), Column("id", Integer, primary_key=True), Column("at", UtcDateTime()))
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE wp05_legacy_naive (id integer PRIMARY KEY, at timestamp)"))
        await connection.execute(text("INSERT INTO wp05_legacy_naive VALUES (1, '2026-09-24 10:00:00.123456')"))
    async with engine.connect() as connection:
        before = (await connection.execute(select(table.c.at))).scalar_one()
    assert before.tzinfo is None  # the hazard the documentation warns about

    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE wp05_legacy_naive ALTER COLUMN at TYPE timestamptz USING at AT TIME ZONE 'UTC'")
        )
        await connection.execute(text("SET LOCAL TimeZone = 'Asia/Tokyo'"))
        written = datetime(2026, 9, 24, 12, 0, 0, 5, tzinfo=PLUS_TWO)
        await connection.execute(table.insert().values(id=2, at=written))
    async with engine.connect() as connection:
        rows = dict((await connection.execute(select(table.c.id, table.c.at).order_by(table.c.id))).tuples().all())
    assert rows == {1: datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=UTC), 2: written}
    assert all(_is_utc(value) for value in rows.values())
