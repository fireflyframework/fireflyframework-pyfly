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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all PyFly entities."""


class SoftDeleteMixin:
    """Mixin that adds a ``deleted_at`` timestamp for soft-delete support.

    Entities using this mixin are never physically removed by
    :class:`SoftDeleteRepository`; instead their ``deleted_at`` column
    is set to the current UTC time.
    """

    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionedMixin:
    """Mixin that enables optimistic locking via a ``version`` column.

    SQLAlchemy will automatically increment the version on every flush
    and raise :class:`sqlalchemy.orm.exc.StaleDataError` when a
    concurrent modification is detected.
    """

    __abstract__ = True

    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @declared_attr  # type: ignore[arg-type]
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"version_id_col": cls.version}


class BaseEntity(Base):
    """Base entity providing ID and audit trail fields.

    All domain entities should inherit from this class to get automatic
    UUID primary keys and created_at/updated_at/created_by/updated_by tracking.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
    )
