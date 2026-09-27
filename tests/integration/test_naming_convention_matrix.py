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
"""One constraint name per constraint on every backend, so one Alembic history runs everywhere (C126).

``Base`` had no naming convention: an unnamed UNIQUE, FOREIGN KEY or CHECK was named by the backend
(``accounts_email_key`` on PostgreSQL, ``email`` on MySQL, nothing on SQLite), so a revision that drops or
changes one ran only on the backend it was authored on. ``Base.metadata`` carries
:data:`~pyfly.data.relational.sqlalchemy.entity.NAMING_CONVENTION` now. These tests reflect the names each
backend actually created, and run the same batch migration (drop a unique constraint, recreate a foreign
key with ``ON DELETE CASCADE``, drop a check) on every lane. The sqlite-file lane runs in the fast suite,
the server lanes in the integration suite.
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    select,
)
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.cli.db import _ENV_PY_TEMPLATE
from pyfly.data.relational.sqlalchemy.entity import Base, use_naming_convention
from pyfly.data.relational.sqlalchemy.naming import ConstraintRename, rename_constraints_to_convention
from tests.support.backend_matrix import RelationalBackend


class ConventionOwner(Base):
    __tablename__ = "nc_owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)


class ConventionAccount(Base):
    __tablename__ = "nc_account"
    __table_args__ = (
        UniqueConstraint("region", "number"),
        CheckConstraint("balance >= 0"),
        CheckConstraint("balance < 1000000", name="nc_account_max_balance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    region: Mapped[str] = mapped_column(String(10))
    number: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(10), index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[int] = mapped_column(ForeignKey("nc_owner.id"))


UNIQUE_EMAIL = "uq_nc_account_email"
UNIQUE_REGION_NUMBER = "uq_nc_account_region_number"
FOREIGN_KEY = "fk_nc_account_owner_id_nc_owner"
NAMED_CHECK = "nc_account_max_balance"
INDEX = "ix_nc_account_code"


def _names(sync: Connection) -> dict[str, Any]:
    inspector = inspect(sync)
    return {  # an unnamed constraint (SQLite before the convention) is ""
        "unique": sorted(u["name"] or "" for u in inspector.get_unique_constraints("nc_account")),
        "foreign_keys": sorted(fk["name"] or "" for fk in inspector.get_foreign_keys("nc_account")),
        "checks": sorted(ck["name"] or "" for ck in inspector.get_check_constraints("nc_account")),
        "indexes": sorted(ix["name"] or "" for ix in inspector.get_indexes("nc_account") if not ix.get("unique")),
    }


async def _created(backend: RelationalBackend) -> AsyncEngine:
    await backend.create_tables(ConventionOwner, ConventionAccount)
    return backend.create_engine()


def _unnamed_check() -> str:
    return next(
        str(constraint.name)
        for constraint in ConventionAccount.__table__.constraints
        if isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "balance >= 0"
    )


async def test_every_backend_creates_the_convention_names(relational_backend: RelationalBackend) -> None:
    engine = await _created(relational_backend)
    async with engine.connect() as connection:
        names = await connection.run_sync(_names)

    assert UNIQUE_EMAIL in names["unique"] and UNIQUE_REGION_NUMBER in names["unique"]
    assert names["foreign_keys"] == [FOREIGN_KEY]
    assert NAMED_CHECK in names["checks"] and _unnamed_check() in names["checks"]
    assert INDEX in names["indexes"]


async def test_one_batch_migration_runs_on_every_backend(relational_backend: RelationalBackend) -> None:
    """The revision a team authors against the convention names (here as Alembic batch operations, the way
    ``pyfly db migrate`` renders them) upgrades SQLite, PostgreSQL, MySQL and MariaDB alike."""
    engine = await _created(relational_backend)

    def upgrade(sync: Connection) -> None:
        context = MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        operations = Operations(context)
        with operations.batch_alter_table("nc_account") as batch:
            batch.drop_constraint(UNIQUE_EMAIL, type_="unique")
            batch.drop_constraint(FOREIGN_KEY, type_="foreignkey")
            batch.create_foreign_key(FOREIGN_KEY, "nc_owner", ["owner_id"], ["id"], ondelete="CASCADE")
            batch.drop_constraint(NAMED_CHECK, type_="check")

    async with engine.begin() as connection:
        await connection.run_sync(upgrade)
    async with engine.connect() as connection:
        names = await connection.run_sync(_names)
    assert UNIQUE_EMAIL not in names["unique"]
    assert names["foreign_keys"] == [FOREIGN_KEY]
    assert NAMED_CHECK not in names["checks"]

    # The schema does what the revision says: duplicates are allowed, and deleting the owner cascades.
    async with engine.begin() as connection:
        await connection.execute(ConventionOwner.__table__.insert().values(id=1))
        for account_id in (1, 2):
            await connection.execute(
                ConventionAccount.__table__.insert().values(
                    id=account_id,
                    email="same@example.com",
                    region="eu",
                    number=account_id,
                    code="c",
                    balance=2_000_000,
                    owner_id=1,
                )
            )
        await connection.execute(ConventionOwner.__table__.delete())
        remaining = (await connection.execute(select(ConventionAccount.__table__.c.id))).all()
    assert remaining == []


# ---------------------------------------------------------------------------------------------------------
# A database created before the convention adopts it with one revision
# ---------------------------------------------------------------------------------------------------------


def _legacy_metadata() -> MetaData:
    """The same two tables as the models, as a pre-26.09.08 ``Base`` created them: no naming convention, so
    every unnamed constraint got its backend's name."""
    legacy = MetaData()
    Table("nc_owner", legacy, Column("id", Integer, primary_key=True, autoincrement=False))
    Table(
        "nc_account",
        legacy,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("email", String(100), nullable=False, unique=True),
        Column("region", String(10), nullable=False),
        Column("number", Integer, nullable=False),
        Column("code", String(10), nullable=False, index=True),
        Column("balance", Integer, nullable=False),
        Column("owner_id", Integer, ForeignKey("nc_owner.id"), nullable=False),
        UniqueConstraint("region", "number"),
        CheckConstraint("balance >= 0"),
        CheckConstraint("balance < 1000000", name="nc_account_max_balance"),
    )
    return legacy


def _primary_key_name(sync: Connection) -> str | None:
    name = inspect(sync).get_pk_constraint("nc_account").get("name")
    return str(name) if name else None


async def test_an_existing_database_adopts_the_convention(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    legacy = _legacy_metadata()
    async with engine.begin() as connection:
        await connection.run_sync(legacy.create_all)
        before = await connection.run_sync(_names)
    assert UNIQUE_EMAIL not in before["unique"]  # the backend named it (or, on SQLite, nobody did)

    def adopt(sync: Connection) -> list[ConstraintRename]:
        operations = Operations(MigrationContext.configure(sync))
        return rename_constraints_to_convention(operations, Base.metadata, tables=["nc_owner", "nc_account"])

    async with engine.begin() as connection:
        renames = await connection.run_sync(adopt)
    assert {rename.new for rename in renames} >= {UNIQUE_EMAIL, UNIQUE_REGION_NUMBER, FOREIGN_KEY, _unnamed_check()}

    async with engine.connect() as connection:
        after = await connection.run_sync(_names)
        primary_key = await connection.run_sync(_primary_key_name)
    assert UNIQUE_EMAIL in after["unique"] and UNIQUE_REGION_NUMBER in after["unique"]
    assert after["foreign_keys"] == [FOREIGN_KEY]
    assert NAMED_CHECK in after["checks"] and _unnamed_check() in after["checks"]
    if relational_backend.dialect in ("postgresql", "sqlite"):  # MySQL and MariaDB call it PRIMARY
        assert primary_key == "pk_nc_account"

    # Adopting twice changes nothing, and a revision written against the convention names now runs.
    async with engine.begin() as connection:
        assert await connection.run_sync(adopt) == []
        await connection.run_sync(_drop_the_email_unique_constraint)
    async with engine.connect() as connection:
        assert UNIQUE_EMAIL not in (await connection.run_sync(_names))["unique"]


def _drop_the_email_unique_constraint(sync: Connection) -> None:
    operations = Operations(MigrationContext.configure(sync, opts={"target_metadata": Base.metadata}))
    with operations.batch_alter_table("nc_account") as batch:
        batch.drop_constraint(UNIQUE_EMAIL, type_="unique")


@pytest.mark.backends("mariadb")
async def test_mariadb_through_a_mysql_url_adopts_the_convention(relational_backend: RelationalBackend) -> None:
    """``mysql+asyncmy://`` against MariaDB (dialect ``mysql``, ``is_mariadb``) is the common form: the same
    renames, MariaDB's own ``DROP CONSTRAINT`` for the check included."""
    engine = create_async_engine(make_url(relational_backend.url).set(drivername="mysql+asyncmy"))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_legacy_metadata().create_all)
        assert engine.dialect.name == "mysql" and engine.dialect.is_mariadb  # known once connected

        def adopt(sync: Connection) -> list[ConstraintRename]:
            operations = Operations(MigrationContext.configure(sync))
            return rename_constraints_to_convention(operations, Base.metadata, tables=["nc_owner", "nc_account"])

        async with engine.begin() as connection:
            assert {rename.kind for rename in await connection.run_sync(adopt)} == {"unique", "foreign_key", "check"}
        async with engine.connect() as connection:
            after = await connection.run_sync(_names)
        assert UNIQUE_EMAIL in after["unique"] and after["foreign_keys"] == [FOREIGN_KEY]
        assert _unnamed_check() in after["checks"]
    finally:
        await engine.dispose()


def _schema_table(metadata: MetaData, schema: str | None) -> Table:
    return Table(
        "nc_schema_account",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("email", String(100), unique=True),
        schema=schema,
    )


def _unique_names(sync: Connection) -> list[str]:
    return sorted(u["name"] or "" for u in inspect(sync).get_unique_constraints("nc_schema_account"))


async def test_a_table_of_another_schema_is_skipped_not_matched_by_name(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """The database is inspected in its default schema. A model table of another schema used to be matched
    with the same-named table of the default schema, and that table's constraints were renamed. It is skipped
    now, with a warning; a table declared with the default schema's name is renamed as before."""
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(_schema_table(MetaData(), None).metadata.create_all)
        before = await connection.run_sync(_unique_names)
        default_schema = await connection.run_sync(lambda sync: inspect(sync).default_schema_name)

    def adopt(schema: str) -> Any:
        def run(sync: Connection) -> list[ConstraintRename]:
            modeled = _schema_table(use_naming_convention(MetaData()), schema).metadata
            return rename_constraints_to_convention(Operations(MigrationContext.configure(sync)), modeled)

        return run

    with caplog.at_level(logging.WARNING, logger="pyfly.data.relational.sqlalchemy.naming"):
        async with engine.begin() as connection:
            assert await connection.run_sync(adopt("pyfly_other_schema")) == []
    async with engine.connect() as connection:
        assert await connection.run_sync(_unique_names) == before
    assert "pyfly_other_schema.nc_schema_account" in caplog.text

    async with engine.begin() as connection:
        renamed = await connection.run_sync(adopt(default_schema))
    assert "uq_nc_schema_account_email" in {rename.new for rename in renamed}
    async with engine.connect() as connection:
        assert await connection.run_sync(_unique_names) == ["uq_nc_schema_account_email"]


# ---------------------------------------------------------------------------------------------------------
# Alembic histories: a legacy one replays on a fresh database, and operations opt in to the convention
# ---------------------------------------------------------------------------------------------------------
#
# Alembic operations name an unnamed constraint with the naming convention of the target_metadata env.py
# gives them. With the convention on Base.metadata itself, a revision written before it (an unnamed unique
# constraint) got the convention's name on a fresh database (CI, a new environment, migrations at startup),
# and a later revision that dropped the constraint by the name its backend had given it failed there.


def _history(root: Path, revisions: list[str], *, env_suffix: str = "") -> AlembicConfig:
    """An Alembic environment in *root*: the ``env.py`` ``pyfly db init`` generates (plus *env_suffix*),
    and one revision per entry of *revisions* (the body of ``upgrade()``), chained in order."""
    versions = root / "versions"
    versions.mkdir(parents=True)
    env = _ENV_PY_TEMPLATE
    if env_suffix:
        env = env.replace("target_metadata = Base.metadata\n", f"target_metadata = Base.metadata\n{env_suffix}\n")
    (root / "env.py").write_text(env)
    previous: str | None = None
    for number, body in enumerate(revisions, start=1):
        revision = f"r{number:03d}"
        (versions / f"{revision}.py").write_text(
            f"import sqlalchemy as sa\nfrom alembic import op\n\nrevision = {revision!r}\n"
            f"down_revision = {previous!r}\nbranch_labels = None\ndepends_on = None\n\n\n"
            f"def upgrade() -> None:\n{textwrap.indent(textwrap.dedent(body).strip(), '    ')}\n"
        )
        previous = revision
    config = AlembicConfig()
    config.set_main_option("script_location", str(root))
    return config


async def _upgrade(config: AlembicConfig, url: str) -> None:
    """``alembic upgrade head``, in a worker thread as ``MigrationRunner`` does: the generated ``env.py``
    runs its own event loop."""
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")


_LEGACY_CREATE = """
op.create_table(
    "legacy_owners",
    sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
op.create_table(
    "legacy_accounts",
    sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
    sa.Column("email", sa.String(length=100), nullable=False),
    sa.Column("owner_id", sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(["owner_id"], ["legacy_owners.id"]),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("email"),
)
"""
"""A revision autogenerated before the convention: every constraint unnamed."""

_BACKEND_UNIQUE_NAME = {"postgresql": "legacy_accounts_email_key", "mysql": "email", "mariadb": "email"}
_BACKEND_FOREIGN_KEY_NAME = {
    "postgresql": "legacy_accounts_owner_id_fkey",
    "mysql": "legacy_accounts_ibfk_1",
    "mariadb": "legacy_accounts_ibfk_1",
}


def _legacy_names(sync: Connection) -> dict[str, Any]:
    inspector = inspect(sync)
    return {
        "unique": sorted(u["name"] for u in inspector.get_unique_constraints("legacy_accounts")),
        "foreign_keys": sorted(fk["name"] for fk in inspector.get_foreign_keys("legacy_accounts")),
    }


@pytest.mark.backends("pg", "mysql", "mariadb")
async def test_a_legacy_history_replays_on_a_fresh_database(
    relational_backend: RelationalBackend, tmp_path: Path
) -> None:
    """The second revision was autogenerated against the database the first one created, before the
    convention: it drops the unique constraint by the name the backend gave it. The generated env.py passes
    ``Base.metadata`` as target_metadata, and the replay reproduces the history's own names."""
    dialect = relational_backend.dialect
    drop = f'op.drop_constraint("{_BACKEND_UNIQUE_NAME[dialect]}", "legacy_accounts", type_="unique")'
    await _upgrade(_history(tmp_path / "migrations", [_LEGACY_CREATE, drop]), relational_backend.url)

    async with relational_backend.create_engine().connect() as connection:
        names = await connection.run_sync(_legacy_names)
    assert names == {"unique": [], "foreign_keys": [_BACKEND_FOREIGN_KEY_NAME[dialect]]}


_OPT_IN = """
from pyfly.data.relational.sqlalchemy.naming import apply_convention_to_operations

apply_convention_to_operations(target_metadata)
"""


async def test_operations_opt_in_to_the_convention(relational_backend: RelationalBackend, tmp_path: Path) -> None:
    """A history that starts with the convention opts its operations in, in env.py: an unnamed constraint
    in a hand-written revision gets the convention's name on every backend, so one revision drops it
    everywhere."""
    create = _LEGACY_CREATE.replace("legacy_", "optin_")
    drop = """
    with op.batch_alter_table("optin_accounts") as batch:
        batch.drop_constraint("uq_optin_accounts_email", type_="unique")
    """
    config = _history(tmp_path / "migrations", [create, drop], env_suffix=_OPT_IN)
    default = Base.metadata.naming_convention
    try:
        await _upgrade(config, relational_backend.url)
    finally:
        Base.metadata.naming_convention = default  # env.py ran in this process

    def names(sync: Connection) -> dict[str, Any]:
        inspector = inspect(sync)
        return {
            "unique": [u["name"] for u in inspector.get_unique_constraints("optin_accounts")],
            "foreign_keys": [fk["name"] for fk in inspector.get_foreign_keys("optin_accounts")],
        }

    async with relational_backend.create_engine().connect() as connection:
        assert await connection.run_sync(names) == {
            "unique": [],
            "foreign_keys": ["fk_optin_accounts_owner_id_optin_owners"],
        }
