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

import asyncio
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, ColumnElement, Integer, MetaData, String, Table, insert, inspect, select, text
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


async def _insert_created(factory: async_sessionmaker[AsyncSession], created: dict[str, datetime]) -> None:
    """One row per label, created at its instant. Written with Core, so no auditing hook re-stamps it."""
    async with factory() as session, session.begin():
        await session.execute(
            insert(TimestampedEvent.__table__),
            [{"label": label, "created_at": at, "updated_at": at} for label, at in created.items()],
        )


async def _labels(factory: async_sessionmaker[AsyncSession], condition: ColumnElement[bool]) -> list[str]:
    async with factory() as session:
        stmt = select(TimestampedEvent.label).where(condition).order_by(TimestampedEvent.label)
        return list((await session.execute(stmt)).scalars().all())


async def _two_days_and_ten_minutes_ago(
    relational_backend: RelationalBackend,
) -> tuple[async_sessionmaker[AsyncSession], datetime]:
    """Rows created two days and ten minutes ago, and the current instant written in +02:00."""
    factory = await _sessions(relational_backend)
    now = datetime.now(UTC)
    await _insert_created(
        factory, {"two days ago": now - timedelta(days=2), "ten minutes ago": now - timedelta(minutes=10)}
    )
    return factory, now.astimezone(PLUS_TWO)


@pytest.mark.backends("pg")
async def test_interval_arithmetic_on_a_timestamp_compares_instants(relational_backend: RelationalBackend) -> None:
    """``created_at + timedelta(days=1) > now``, typical of expiry and retention queries: the timedelta is
    bound as an ``INTERVAL``, as it is beside a plain ``DateTime``. Bound as a ``UtcDateTime`` it made
    ``timestamptz + timestamptz``, which PostgreSQL rejects. SQLite, MySQL and MariaDB have no interval
    arithmetic in SQLAlchemy: the next test is the portable form."""
    factory, now = await _two_days_and_ten_minutes_ago(relational_backend)
    created_at = TimestampedEvent.created_at

    assert await _labels(factory, created_at + timedelta(days=1) > now) == ["ten minutes ago"]
    assert await _labels(factory, created_at + timedelta(hours=1) < now) == ["two days ago"]
    assert await _labels(factory, created_at - timedelta(hours=1) > now - timedelta(hours=3)) == ["ten minutes ago"]
    assert await _labels(factory, created_at.between(now - timedelta(hours=1), now)) == ["ten minutes ago"]


async def test_a_shifted_bound_compares_instants_on_every_backend(relational_backend: RelationalBackend) -> None:
    """The portable form of the queries above shifts the parameter, not the column: the bound is a datetime,
    normalized like any other, and the column stays bare, so an index on it can serve the query. SQLAlchemy
    compiles ``created_at + timedelta(...)`` on SQLite, MySQL and MariaDB to a numeric addition, which
    matches the wrong rows (it always has, with a plain ``DateTime`` too)."""
    factory, now = await _two_days_and_ten_minutes_ago(relational_backend)
    created_at = TimestampedEvent.created_at

    assert await _labels(factory, created_at > now - timedelta(days=1)) == ["ten minutes ago"]
    assert await _labels(factory, created_at < now - timedelta(hours=1)) == ["two days ago"]
    assert await _labels(factory, created_at.between(now - timedelta(hours=1), now)) == ["ten minutes ago"]


@pytest.mark.backends("sqlite-file", "mysql", "mariadb")
async def test_a_string_compared_with_a_timestamp_is_bound_as_a_string(relational_backend: RelationalBackend) -> None:
    """``created_at > '2020-01-01'`` is bound as a string, as it is beside a plain ``DateTime``, and the backend
    compares it. Bound as a ``UtcDateTime`` it raised ``TypeError`` on SQLite. (asyncpg refuses a string for a
    PostgreSQL timestamp either way.)"""
    factory, _ = await _two_days_and_ten_minutes_ago(relational_backend)

    assert await _labels(factory, TimestampedEvent.created_at > "2020-01-01") == ["ten minutes ago", "two days ago"]
    assert await _labels(factory, TimestampedEvent.created_at < "2020-01-01") == []


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
    adopts it (``update_type_annotation_map`` makes every ``Mapped[datetime]`` one) reads back its wall
    times as UTC, aware like every other value of the type, but the server converts the writes bound to it
    with the session's ``TimeZone``. The documented migration reads the old values as the UTC wall times
    they are."""
    table = Table("wp05_legacy_naive", MetaData(), Column("id", Integer, primary_key=True), Column("at", UtcDateTime()))
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE wp05_legacy_naive (id integer PRIMARY KEY, at timestamp)"))
        await connection.execute(text("INSERT INTO wp05_legacy_naive VALUES (1, '2026-09-24 10:00:00.123456')"))
    async with engine.connect() as connection:
        before = (await connection.execute(select(table.c.at))).scalar_one()
    assert before == datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=UTC) and _is_utc(before)

    tokyo_write = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL TimeZone = 'Asia/Tokyo'"))
        await connection.execute(table.insert().values(id=3, at=tokyo_write))
    async with engine.connect() as connection:
        drifted = (await connection.execute(select(table.c.at).where(table.c.id == 3))).scalar_one()
    assert drifted == tokyo_write + timedelta(hours=9)  # the hazard the documentation warns about
    async with engine.begin() as connection:
        await connection.execute(table.delete().where(table.c.id == 3))

    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE wp05_legacy_naive ALTER COLUMN at TYPE timestamptz USING at AT TIME ZONE 'UTC'")
        )
        await connection.execute(text("SET LOCAL TimeZone = 'Asia/Tokyo'"))
        written = datetime(2026, 9, 24, 12, 0, 0, 5, tzinfo=PLUS_TWO)
        await connection.execute(table.insert().values(id=2, at=written))
    async with engine.connect() as connection:
        rows = dict((await connection.execute(select(table.c.id, table.c.at).order_by(table.c.id))).all())
    assert rows == {1: datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=UTC), 2: written}
    assert all(_is_utc(value) for value in rows.values())


# ---------------------------------------------------------------------------------------------------------
# Migrations: the revision `pyfly db migrate` renders for a BaseEntity table runs
# ---------------------------------------------------------------------------------------------------------

_PROJECT_MODELS = """\
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import BaseEntity, SoftDeleteMixin, UtcDateTime


class Widget(SoftDeleteMixin, BaseEntity):
    __tablename__ = "wp05_migrated_widget"

    name: Mapped[str] = mapped_column(String(50))
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(strict=True), default=None)
"""
"""A project's ``models.py``: the framework's ``UtcDateTime`` columns and one the application declares."""

_TIMESTAMP_COLUMNS = ("created_at", "updated_at", "deleted_at", "due_at")


def _pyfly(project: Path, *args: str) -> None:
    """``pyfly <args>`` in *project*, in a process of its own as a developer runs it: ``Base.metadata`` holds
    the project's models only, and Alembic imports the revision it renders there."""
    result = subprocess.run(
        [sys.executable, "-c", "from pyfly.cli.main import cli; cli()", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"pyfly {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


def _configure(project: Path, url: str) -> None:
    """What a project does after ``pyfly db init``: point ``alembic.ini`` at its database, and import its
    models in ``env.py`` so that autogenerate sees their tables."""
    ini = project / "alembic.ini"
    configured = re.sub(r"(?m)^sqlalchemy\.url = .*$", f"sqlalchemy.url = {url.replace('%', '%%')}", ini.read_text())
    assert configured != ini.read_text()
    ini.write_text(configured)
    env = project / "alembic" / "env.py"
    anchor = "target_metadata = Base.metadata\n"
    assert anchor in env.read_text()
    env.write_text(env.read_text().replace(anchor, f"import models  # noqa: E402, F401\n\n{anchor}"))


def _upgrade_body(revision: Path) -> str:
    return revision.read_text().split("def upgrade")[1].split("def downgrade")[0]


async def test_an_autogenerated_revision_of_a_base_entity_table_upgrades_the_database(
    relational_backend: RelationalBackend, tmp_path: Path
) -> None:
    """Alembic renders a ``UtcDateTime`` column as ``pyfly.data.relational.sqlalchemy.types.UtcDateTime()`` and
    imports nothing for it, so ``pyfly db upgrade`` died on the revision of every BaseEntity table with
    ``NameError: name 'pyfly' is not defined``. The generated ``env.py`` passes
    :func:`~pyfly.data.relational.sqlalchemy.types.render_item`, which adds the import. The type stays
    ``UtcDateTime`` in the revision, so MySQL and MariaDB get ``DATETIME(6)``, and a second revision finds
    nothing to change."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "models.py").write_text(_PROJECT_MODELS)
    await asyncio.to_thread(_pyfly, project, "db", "init")
    _configure(project, relational_backend.url)
    versions = project / "alembic" / "versions"

    await asyncio.to_thread(_pyfly, project, "db", "migrate", "-m", "widgets")
    (revision,) = versions.glob("*_widgets.py")
    assert "pyfly.data.relational.sqlalchemy.types.UtcDateTime(strict=True)" in _upgrade_body(revision)
    await asyncio.to_thread(_pyfly, project, "db", "upgrade")

    engine = relational_backend.create_engine()
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                column["name"]: column["type"] for column in inspect(sync).get_columns("wp05_migrated_widget")
            }
        )
    assert set(_TIMESTAMP_COLUMNS) <= set(columns)
    for name in _TIMESTAMP_COLUMNS:
        if relational_backend.dialect in ("mysql", "mariadb"):
            assert getattr(columns[name], "fsp", None) == 6, (name, columns[name])
        elif relational_backend.dialect == "postgresql":
            assert getattr(columns[name], "timezone", None) is True, (name, columns[name])

    await asyncio.to_thread(_pyfly, project, "db", "migrate", "-m", "unchanged")
    (unchanged,) = versions.glob("*_unchanged.py")
    assert "op." not in _upgrade_body(unchanged), unchanged.read_text()
