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

:class:`Base` names every constraint its tables leave unnamed with :data:`NAMING_CONVENTION`, so each one
has one deterministic name on every backend (in ``create_all()`` and in the revisions Alembic autogenerates,
which spell the names out), and one Alembic history runs on SQLite, PostgreSQL, MySQL and MariaDB alike.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Unicode,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.schema import conv
from sqlalchemy.sql.schema import ColumnCollectionConstraint, Constraint, SchemaItem

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
"""The constraint naming convention of :class:`Base`'s tables, as a SQLAlchemy ``naming_convention``.

An unnamed constraint gets:

- ``uq_<table>_<column>_<column>...`` for a UNIQUE constraint (``unique=True`` included);
- ``fk_<table>_<column>..._<referred table>`` for a FOREIGN KEY;
- ``ck_<table>_<hash of its SQL>`` for a CHECK;
- ``pk_<table>`` for the PRIMARY KEY (MySQL and MariaDB always call it ``PRIMARY``);
- ``ix_<table>_<column>`` for an index (``index=True``), as SQLAlchemy has always named them.

Names are at most :data:`MAX_CONSTRAINT_NAME_LENGTH` characters on every backend. A constraint the
application names keeps its name. The CHECK that a ``Boolean(create_constraint=True)`` or a non-native
``Enum(create_constraint=True)`` column creates is named by SQLAlchemy, not by the convention: it stays unnamed
for a ``Boolean``, and takes the enum type's name for an ``Enum``. Name those types (``name=``) when a
revision has to refer to their checks.

It names the constraints of the *models* (:func:`use_naming_convention`), not the ones Alembic operations
create: ``Base.metadata.naming_convention``, which Alembic applies to the unnamed constraints of a revision's
operations, stays SQLAlchemy's default, so a history written before 26.09.08 replays on a fresh database with
the names it always had. A history written under the convention opts its operations in with
:func:`pyfly.data.relational.sqlalchemy.naming.apply_convention_to_operations`. A database created before
26.09.08 keeps the names its backend gave it until it adopts the convention with the one-time rename
migration (:func:`pyfly.data.relational.sqlalchemy.naming.rename_constraints_to_convention`).
"""

_CONVENTION_INFO_KEY = "pyfly.naming_convention"


def use_naming_convention(metadata: MetaData) -> MetaData:
    """Name the unnamed constraints of the tables of *metadata* with :data:`NAMING_CONVENTION` from now on
    (``Base.metadata`` does), and return it.

    The names are set on the constraints as the tables are declared (``create_all()`` renders them, and an
    autogenerated revision spells them out with ``op.f()``), while ``metadata.naming_convention``, the one
    Alembic operations apply, is left as it is.
    """
    metadata.info[_CONVENTION_INFO_KEY] = True
    return metadata


def uses_naming_convention(metadata: MetaData) -> bool:
    """Whether *metadata* names its tables' constraints with :data:`NAMING_CONVENTION`
    (:func:`use_naming_convention`)."""
    return bool(metadata.info.get(_CONVENTION_INFO_KEY))


def _convention_name(constraint: Constraint, table: Table) -> str | None:
    if isinstance(constraint, PrimaryKeyConstraint):
        return _primary_key_name(constraint, table)
    if isinstance(constraint, UniqueConstraint):
        return _unique_name(constraint, table)
    if isinstance(constraint, ForeignKeyConstraint):
        return _foreign_key_name(constraint, table)
    if isinstance(constraint, CheckConstraint):
        return _check_name(constraint, table)
    return None


@event.listens_for(Constraint, "after_parent_attach")
def _name_constraint(constraint: Constraint, parent: SchemaItem) -> None:
    """Name an unnamed constraint as it joins a table of a :func:`use_naming_convention` metadata (the way
    SQLAlchemy applies a ``naming_convention``); ``conv`` marks the name as final."""
    if isinstance(parent, Column):
        # A CHECK declared on a column: named when the column joins its table.
        event.listen(parent, "after_parent_attach", lambda _column, table: _name_constraint(constraint, table))
        return
    if not isinstance(parent, Table) or constraint.name is not None or not uses_naming_convention(parent.metadata):
        return
    name = _convention_name(constraint, parent)
    if name is not None:
        constraint.name = conv(name)


@event.listens_for(Column, "after_parent_attach")
def _name_primary_key(column: Column[Any], table: SchemaItem) -> None:
    """Name the table's implicit primary key again once a key column has joined it: SQLAlchemy resets that
    name to whatever the metadata's own ``naming_convention`` gives it (nothing, by default)."""
    if not isinstance(table, Table) or not column.primary_key or not uses_naming_convention(table.metadata):
        return
    if table.primary_key.name is None:
        table.primary_key.name = conv(_primary_key_name(table.primary_key, table))


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

    Its ``metadata`` names every unnamed constraint of its tables with :data:`NAMING_CONVENTION`
    (:func:`use_naming_convention`). A
    :class:`VersionedMixin` entity that declares its own ``__mapper_args__`` (``polymorphic_on`` on an
    inheritance root, ``eager_defaults``), or inherits them from an abstract base or another mixin, keeps
    its optimistic locking whatever the order of its bases: the declarations are merged and the mixin's
    ``version_id_col`` is added.
    """

    metadata = use_naming_convention(MetaData())

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

    A soft-deleted row is invisible to every ORM load of every session: repository reads,
    ``session.get()``, relationship loads and joins. Opt out with the ``include_deleted`` execution option
    or :func:`~pyfly.data.relational.sqlalchemy.soft_delete_criteria.including_deleted`.
    """

    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None, nullable=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Soft-deleted rows are invisible to every ORM load of every session from the first soft-delete
        # entity on (pyfly.data.relational.sqlalchemy.soft_delete_criteria).
        from pyfly.data.relational.sqlalchemy.soft_delete_criteria import install_soft_delete_criteria

        install_soft_delete_criteria()

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionedMixin:
    """Mixin that enables optimistic locking via a ``version`` column.

    SQLAlchemy will automatically increment the version on every flush
    and raise :class:`sqlalchemy.orm.exc.StaleDataError` when a
    concurrent modification is detected; a repository call, or the commit of a unit
    of work, raises it as :class:`~pyfly.kernel.exceptions.OptimisticLockingFailureException`.

    The entity, and its abstract bases or other mixins, may declare their own ``__mapper_args__`` (a dict,
    or a ``declared_attr``), in any base order: they are merged, the first in the MRO winning a key, and
    ``version_id_col`` is added. Declaring ``version_id_col`` there as well is a conflict, and mapping the
    class raises ``TypeError``. Subclasses in an inheritance hierarchy share the root's version column.
    """

    __abstract__ = True

    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @declared_attr  # type: ignore[arg-type]
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"version_id_col": cls.version}


_OWN_MAPPER_ARGS = "__pyfly_own_mapper_args__"
"""Where the merged ``__mapper_args__`` directive installed on a class keeps what that class itself
declared, so a subclass merging again sees the original."""


def _keep_version_id_col(cls: type[Base]) -> None:
    """Merge :class:`VersionedMixin`'s ``version_id_col`` into the ``__mapper_args__`` the entity gets from
    itself and its bases.

    Declarative takes ``__mapper_args__`` from the first class of the MRO that has it, so an entity's own
    arguments, or an abstract base's placed before the mixin, shadowed the mixin's and the mapper got no
    version column: optimistic locking was silently off (C123). And with the mixin first, the abstract
    base's arguments were lost instead. Every declaration in the MRO is merged, the first in MRO order
    winning a key, as attribute lookup would; a mapped (non-abstract) base's plain dict is left out, since
    declarative does not inherit it. A class whose mapped ancestor is versioned already is left alone: its
    mapper inherits the ancestor's version column.
    """
    if not issubclass(cls, VersionedMixin):
        return
    if any(issubclass(base, VersionedMixin) and "__mapper__" in vars(base) for base in cls.__mro__[1:]):
        return
    declared = _declared_mapper_args(cls)
    if not declared:
        return  # only the mixin's own
    for owner, args in declared:
        if getattr(args, "fget", None) is None:
            if not isinstance(args, Mapping):
                raise TypeError(f"{owner.__name__}.__mapper_args__ must be a dict or a declared_attr, got {args!r}")
            _refuse_own_version_id_col(cls.__name__, args)

    def mapper_args(entity: type[Base]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for _owner, args in reversed(declared):
            produce = getattr(args, "fget", None)
            merged.update(produce(entity) if produce is not None else args)
        _refuse_own_version_id_col(entity.__name__, merged)
        merged["version_id_col"] = entity.version  # type: ignore[attr-defined]
        return merged

    setattr(mapper_args, _OWN_MAPPER_ARGS, vars(cls).get("__mapper_args__"))
    cls.__mapper_args__ = declared_attr.directive(mapper_args)


def _declared_mapper_args(cls: type) -> list[tuple[type, Any]]:
    """Every ``__mapper_args__`` declared along *cls*'s MRO other than :class:`VersionedMixin`'s, in MRO
    order: ``(declaring class, dict or declared_attr)``."""
    declared: list[tuple[type, Any]] = []
    for base in cls.__mro__:
        if base is VersionedMixin or "__mapper_args__" not in vars(base):
            continue
        args = vars(base)["__mapper_args__"]
        produce = getattr(args, "fget", None)
        if produce is not None and hasattr(produce, _OWN_MAPPER_ARGS):
            # A merged directive this hook installed on an abstract base: what that base declared itself
            # (its own bases come next in the MRO anyway).
            args = getattr(produce, _OWN_MAPPER_ARGS)
            if args is None:
                continue
            produce = getattr(args, "fget", None)
        if base is not cls and "__mapper__" in vars(base) and produce is None:
            continue  # a mapped class's plain dict is its own; declarative does not inherit it
        declared.append((base, args))
    return declared


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
