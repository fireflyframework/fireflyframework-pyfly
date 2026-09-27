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
"""Base entity with audit fields for all domain entities."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from pyfly.data.relational.sqlalchemy.types import UtcDateTime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all PyFly entities.

    A :class:`VersionedMixin` entity that declares its own ``__mapper_args__`` (``polymorphic_on`` on an
    inheritance root, ``eager_defaults``) keeps its optimistic locking: the mixin's ``version_id_col`` is
    merged into the entity's arguments.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        _keep_version_id_col(cls)
        super().__init_subclass__(**kwargs)


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
        String(255),
        default=None,
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
    )
