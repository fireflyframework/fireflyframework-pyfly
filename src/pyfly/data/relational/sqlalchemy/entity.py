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
"""Base entity with audit fields for all domain entities.

:class:`Base` carries :data:`NAMING_CONVENTION`, so every constraint the application leaves unnamed gets
one deterministic name on every backend, and one Alembic history runs on SQLite, PostgreSQL, MySQL and
MariaDB alike.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, MetaData, Table, Unicode, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.sql.schema import ColumnCollectionConstraint, Constraint

from pyfly.data.relational.sqlalchemy.types import UtcDateTime


def _utc_now() -> datetime:
    return datetime.now(UTC)


MAX_CONSTRAINT_NAME_LENGTH = 63
"""The longest constraint name the convention produces: PostgreSQL's identifier limit, the tightest of the
supported backends. A longer name is cut and suffixed with a hash of the full name, the same on every
backend (left to the dialects, PostgreSQL and MySQL would each truncate it their own way)."""


def _bounded(name: str) -> str:
    if len(name) <= MAX_CONSTRAINT_NAME_LENGTH:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[: MAX_CONSTRAINT_NAME_LENGTH - len(digest) - 1]}_{digest}"


def _column_names(constraint: Constraint) -> list[str]:
    if isinstance(constraint, ForeignKeyConstraint):
        return [element.parent.name for element in constraint.elements]
    if isinstance(constraint, ColumnCollectionConstraint):
        return [column.name for column in constraint.columns]
    return []


def _unique_name(constraint: Constraint, table: Table) -> str:
    return _bounded("_".join(["uq", table.name, *_column_names(constraint)]))


def _foreign_key_name(constraint: Constraint, table: Table) -> str:
    assert isinstance(constraint, ForeignKeyConstraint)
    referred = constraint.elements[0].target_fullname.split(".")[-2]
    return _bounded("_".join(["fk", table.name, *_column_names(constraint), referred]))


def _check_name(constraint: Constraint, table: Table) -> str:
    # A CHECK has no column list to name it by: an unnamed one is named after its SQL text. Name your
    # CHECK constraints to get readable names (an explicit name is kept as it is).
    assert isinstance(constraint, CheckConstraint)
    digest = hashlib.sha256(str(constraint.sqltext).encode()).hexdigest()[:8]
    return _bounded(f"ck_{table.name}_{digest}")


def _primary_key_name(constraint: Constraint, table: Table) -> str:
    return _bounded(f"pk_{table.name}")


NAMING_CONVENTION: dict[str, Any] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "%(pyfly_unique_name)s",
    "ck": "%(pyfly_check_name)s",
    "fk": "%(pyfly_foreign_key_name)s",
    "pk": "%(pyfly_primary_key_name)s",
    "pyfly_unique_name": _unique_name,
    "pyfly_check_name": _check_name,
    "pyfly_foreign_key_name": _foreign_key_name,
    "pyfly_primary_key_name": _primary_key_name,
}
"""The constraint naming convention of :class:`Base` (``Base.metadata.naming_convention``).

An unnamed constraint gets:

- ``uq_<table>_<column>_<column>...`` for a UNIQUE constraint (``unique=True`` included);
- ``fk_<table>_<column>..._<referred table>`` for a FOREIGN KEY;
- ``ck_<table>_<hash of its SQL>`` for a CHECK;
- ``pk_<table>`` for the PRIMARY KEY (MySQL and MariaDB always call it ``PRIMARY``);
- ``ix_<table>_<column>`` for an index (``index=True``), as SQLAlchemy has always named them.

Names are at most :data:`MAX_CONSTRAINT_NAME_LENGTH` characters on every backend. A constraint the
application names keeps its name. A table created before 26.09.08 keeps the names its backend gave it: see
the relational module documentation for the one-time rename migration.
"""


def _uncluster_random_key(cls: type[Base]) -> None:
    """Make a random UUID primary key (:class:`BaseEntity`'s ``id``: a ``Uuid`` column with a Python-side
    default) ``NONCLUSTERED`` on SQL Server; the dialect option is ignored elsewhere. An entity whose key is
    something else (an identity integer, a key it assigns itself) keeps the default clustering."""
    table = vars(cls).get("__table__")
    if not isinstance(table, Table):
        return
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        return
    key = columns[0]
    if isinstance(key.type, Uuid) and key.default is not None and key.default.is_callable:
        table.primary_key.dialect_kwargs["mssql_clustered"] = False


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all PyFly entities.

    Its ``metadata`` names every unnamed constraint with :data:`NAMING_CONVENTION`. A
    :class:`VersionedMixin` entity that declares its own ``__mapper_args__`` (``polymorphic_on`` on an
    inheritance root, ``eager_defaults``) keeps its optimistic locking: the mixin's ``version_id_col`` is
    merged into the entity's arguments.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        _keep_version_id_col(cls)
        super().__init_subclass__(**kwargs)
        _uncluster_random_key(cls)


class SoftDeleteMixin:
    """Mixin that adds a ``deleted_at`` timestamp for soft-delete support.

    Entities using this mixin are never physically removed by
    :class:`SoftDeleteRepository`; instead their ``deleted_at`` column
    is set to the current UTC time (a :class:`~pyfly.data.relational.sqlalchemy.types.UtcDateTime`:
    aware UTC with microseconds on every backend).
    """

    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionedMixin:
    """Mixin that enables optimistic locking via a ``version`` column.

    SQLAlchemy will automatically increment the version on every flush
    and raise :class:`sqlalchemy.orm.exc.StaleDataError` when a
    concurrent modification is detected.

    The entity may declare its own ``__mapper_args__`` (a dict, or a ``declared_attr``): ``version_id_col``
    is merged into them. Declaring ``version_id_col`` there as well is a conflict, and mapping the class
    raises ``TypeError``. Subclasses in an inheritance hierarchy share the root's version column.
    """

    __abstract__ = True

    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @declared_attr  # type: ignore[arg-type]
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"version_id_col": cls.version}


def _keep_version_id_col(cls: type[Base]) -> None:
    """Merge :class:`VersionedMixin`'s ``version_id_col`` into the entity's own ``__mapper_args__``.

    The entity's own attribute shadows the mixin's, so without this the mapper got no version column and
    optimistic locking was silently off (C123). A class whose mapped ancestor is versioned already is left
    alone: its mapper inherits the ancestor's version column.
    """
    if not issubclass(cls, VersionedMixin) or "__mapper_args__" not in vars(cls):
        return
    if any(issubclass(base, VersionedMixin) and "__mapper__" in vars(base) for base in cls.__mro__[1:]):
        return
    own = vars(cls)["__mapper_args__"]
    produce = getattr(own, "fget", None)
    if produce is None:
        if not isinstance(own, Mapping):
            raise TypeError(f"{cls.__name__}.__mapper_args__ must be a dict or a declared_attr, got {own!r}")
        _refuse_own_version_id_col(cls.__name__, own)

    def mapper_args(entity: type[Base]) -> dict[str, Any]:
        args = dict(produce(entity) if produce is not None else own)
        _refuse_own_version_id_col(entity.__name__, args)
        args["version_id_col"] = entity.version  # type: ignore[attr-defined]
        return args

    cls.__mapper_args__ = declared_attr.directive(mapper_args)


def _refuse_own_version_id_col(name: str, args: Mapping[str, Any]) -> None:
    if "version_id_col" in args:
        raise TypeError(
            f"{name} uses VersionedMixin, which supplies version_id_col, and declares version_id_col in its "
            "own __mapper_args__ too: remove one of them"
        )


class BaseEntity(Base):
    """Base entity providing ID and audit trail fields.

    All domain entities should inherit from this class to get automatic
    UUID primary keys and created_at/updated_at/created_by/updated_by tracking.
    The timestamps are :class:`~pyfly.data.relational.sqlalchemy.types.UtcDateTime` columns: aware UTC
    with microseconds on every backend, after a reload too.

    On SQL Server the user columns are ``NVARCHAR`` (a ``VARCHAR`` stores characters outside the database
    code page as ``?``), and the random UUID key is a ``NONCLUSTERED`` primary key (as the clustered index,
    every insert would land on a random page). The DDL of every other backend is unchanged.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        default=_utc_now,
        onupdate=_utc_now,
    )
    created_by: Mapped[str | None] = mapped_column(
        Unicode(255),
        default=None,
    )
    updated_by: Mapped[str | None] = mapped_column(
        Unicode(255),
        default=None,
    )
