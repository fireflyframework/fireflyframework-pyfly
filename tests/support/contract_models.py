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
"""Contract models: the mappings every repository contract test runs on, on every backend lane.

The repository suites used to run on one flat table on ``:memory:`` SQLite with foreign keys off, so
cascades, relationship loading, optimistic locking and composite keys were never exercised (C161).
These models cover exactly those shapes, and they are portable by construction: every string has a
length (MySQL/MariaDB need one), the integer key is an ``Identity()`` column (IDENTITY on PostgreSQL,
SQL Server and Oracle, AUTO_INCREMENT on MySQL/MariaDB), and the foreign key has no ``ON DELETE``
action, so only the ORM cascade removes children and a bulk delete that bypasses it fails on every lane.

- :class:`ContractParent` / :class:`ContractChild`: one-to-many with ``cascade="all, delete-orphan"``.
  The parent is a :class:`BaseEntity` (UUID key assigned in Python, audit columns); the child has a
  database-generated integer key, so both key strategies are covered.
- :class:`ContractVersioned`: optimistic locking through :class:`VersionedMixin`.
- :class:`ContractLine`: a composite primary key and no surrogate id.
- :class:`ContractSoftItem`: soft delete through :class:`SoftDeleteMixin`.

The tables live in PyFly's shared ``Base.metadata``, as application entities do. Create only the
tables a test needs with ``await relational_backend.create_tables(*CONTRACT_MODELS)``: in a full run
``Base.metadata`` also holds every other test module's models, and some of those are not portable.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Identity, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin, VersionedMixin


class ContractParent(BaseEntity):
    """The aggregate root of the parent/child contract: a UUID key and the audit columns."""

    __tablename__ = "contract_parent"

    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    children: Mapped[list[ContractChild]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="ContractChild.position",
    )


class ContractChild(Base):
    """A child row: database-generated integer key and a plain foreign key to its parent."""

    __tablename__ = "contract_child"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contract_parent.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, default=0)
    parent: Mapped[ContractParent] = relationship(back_populates="children")


class ContractVersioned(VersionedMixin, BaseEntity):
    """An optimistically locked entity: every flush of a change bumps ``version``."""

    __tablename__ = "contract_versioned"

    name: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer, default=0)


class ContractLine(Base):
    """An order line keyed by ``(order_code, line_no)``: a composite key with no surrogate id."""

    __tablename__ = "contract_line"

    order_code: Mapped[str] = mapped_column(String(36), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    sku: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class ContractSoftItem(SoftDeleteMixin, BaseEntity):
    """A soft-deletable entity: removal sets ``deleted_at`` instead of deleting the row."""

    __tablename__ = "contract_soft_item"

    label: Mapped[str] = mapped_column(String(100))


CONTRACT_MODELS: tuple[type[Base], ...] = (
    ContractParent,
    ContractChild,
    ContractVersioned,
    ContractLine,
    ContractSoftItem,
)
"""Every contract model, parents before children (``create_all`` sorts them again anyway)."""
