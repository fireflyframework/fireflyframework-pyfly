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

from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base
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
    return {
        "unique": sorted(u["name"] for u in inspector.get_unique_constraints("nc_account")),
        "foreign_keys": sorted(fk["name"] for fk in inspector.get_foreign_keys("nc_account")),
        "checks": sorted(ck["name"] for ck in inspector.get_check_constraints("nc_account")),
        "indexes": sorted(ix["name"] for ix in inspector.get_indexes("nc_account") if not ix.get("unique")),
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
