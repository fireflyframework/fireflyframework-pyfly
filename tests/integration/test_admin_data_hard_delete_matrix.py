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
"""The admin data API deletes a record whose dependents include soft-deleted rows, on every backend.

The soft-delete loader criteria hide a soft-deleted child from the collection load that a delete cascade
(or the unlinking of a nullable foreign key, or of many-to-many link rows) issues. The admin provider
deleted a plain record with ``session.delete()``, so the soft-deleted children stayed behind and the
record's ``DELETE`` violated their foreign key: the admin answered 409 for any aggregate with a
soft-deleted child. It deletes through ``hard_delete()`` now, which loads what the delete reaches with
the deleted rows. The sqlite-file lane (foreign keys on) runs in the fast suite, the server lanes in the
integration suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, ForeignKey, String, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
from pyfly.admin.data.models import AdminOperationContext, ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.admin.data.service import AdminDataService
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import including_deleted
from pyfly.security.context import SecurityContext
from tests.support.backend_matrix import RelationalBackend

admin_topic_labels = Table(
    "adm_hd_topic_label",
    Base.metadata,
    Column("topic_id", ForeignKey("adm_hd_topic.id"), primary_key=True),
    Column("label_id", ForeignKey("adm_hd_label.id"), primary_key=True),
)


class AdminTopic(BaseEntity):
    """A plain record: its posts cascade, its notes are unlinked, its labels are linked through a table."""

    __tablename__ = "adm_hd_topic"

    title: Mapped[str] = mapped_column(String(50))
    posts: Mapped[list[AdminTopicPost]] = relationship(cascade="all, delete-orphan")
    notes: Mapped[list[AdminTopicNote]] = relationship()
    labels: Mapped[list[AdminLabel]] = relationship(secondary=admin_topic_labels)


class AdminTopicPost(SoftDeleteMixin, BaseEntity):
    __tablename__ = "adm_hd_topic_post"

    body: Mapped[str] = mapped_column(String(50))
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adm_hd_topic.id"))


class AdminTopicNote(SoftDeleteMixin, BaseEntity):
    """No delete cascade: deleting the topic sets ``topic_id`` to NULL."""

    __tablename__ = "adm_hd_topic_note"

    body: Mapped[str] = mapped_column(String(50))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("adm_hd_topic.id"), nullable=True)


class AdminLabel(SoftDeleteMixin, BaseEntity):
    __tablename__ = "adm_hd_label"

    name: Mapped[str] = mapped_column(String(50))


def _admin(factory: async_sessionmaker[AsyncSession]) -> tuple[AdminDataService, AdminOperationContext]:
    registry = AdminResourceRegistry()
    registry.register(
        ModelAdmin(
            "topics",
            AdminTopic,
            fields=("id", "title"),
            editable_fields=("title",),
            operations=("list", "read", "create", "update", "delete"),
            provider=SqlAlchemyAdminProvider(factory, edit_token_key="hard-delete-test-key-of-32-bytes-or-more"),
        )
    )
    return AdminDataService(registry), AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))


async def test_the_admin_deletes_a_record_whose_dependents_include_soft_deleted_rows(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(AdminTopic, AdminTopicPost, AdminTopicNote, AdminLabel, admin_topic_labels)
    factory = async_sessionmaker(relational_backend.create_engine(), expire_on_commit=False)
    service, actor = _admin(factory)
    created = await service.create("topics", {"title": "t"}, actor)
    topic_id = uuid.UUID(created.id)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        topic = await session.get(AdminTopic, topic_id)
        assert topic is not None
        session.add_all(
            [
                AdminTopicPost(body="live", topic_id=topic_id),
                AdminTopicPost(body="dead", topic_id=topic_id, deleted_at=now),
                AdminTopicNote(body="live", topic_id=topic_id),
                AdminTopicNote(body="dead", topic_id=topic_id, deleted_at=now),
            ]
        )
        await session.run_sync(
            lambda _sync: topic.labels.extend([AdminLabel(name="live"), AdminLabel(name="dead", deleted_at=now)])
        )

    saved = await service.get("topics", created.id, actor)
    await service.delete("topics", created.id, saved.edit_token, actor)

    async with factory() as session:
        with including_deleted():
            assert await session.get(AdminTopic, topic_id) is None
            assert (await session.execute(select(func.count()).select_from(AdminTopicPost))).scalar_one() == 0
            notes = (await session.execute(select(AdminTopicNote.body, AdminTopicNote.topic_id))).all()
            assert sorted(notes) == [("dead", None), ("live", None)]
            links = (await session.execute(select(func.count()).select_from(admin_topic_labels))).scalar_one()
            assert links == 0
            labels = (await session.execute(select(AdminLabel.name).order_by(AdminLabel.name))).scalars().all()
            assert labels == ["dead", "live"]  # the link rows go, the labels stay
