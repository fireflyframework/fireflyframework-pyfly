"""Real PostgreSQL locking and existing PyFly entity/mixin integration."""

import asyncio
from uuid import uuid4

from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
from pyfly.admin.data.models import AdminOperationContext, AdminQuery, ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.admin.data.service import AdminDataService
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin, VersionedMixin
from pyfly.kernel.exceptions import ConflictException
from pyfly.security.context import SecurityContext


class AdminPgItem(SoftDeleteMixin, VersionedMixin, BaseEntity):
    __tablename__ = "admin_it_" + uuid4().hex
    name: Mapped[str] = mapped_column(String(80), unique=True)


async def test_postgres_uuid_locking_and_soft_delete(pg_url):
    engine = create_async_engine(pg_url)
    async with engine.begin() as connection:
        await connection.run_sync(AdminPgItem.__table__.create)
    try:
        registry = AdminResourceRegistry()
        registry.register(
            ModelAdmin(
                "items",
                AdminPgItem,
                fields=("id", "name", "version", "updated_at"),
                editable_fields=("name",),
                operations=("list", "read", "create", "update", "delete"),
                provider=SqlAlchemyAdminProvider(
                    async_sessionmaker(engine, expire_on_commit=False),
                    edit_token_key="postgres-test-key-of-at-least-32-bytes",
                ),
            )
        )
        service = AdminDataService(registry)
        actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
        created = await service.create("items", {"name": "First"}, actor)
        results = await asyncio.gather(
            *(
                service.update("items", created.id, {"name": name}, created.edit_token, actor)
                for name in ("Second", "Third")
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(r, ConflictException) for r in results) == 1, results
        saved = await service.get("items", created.id, actor)
        assert saved.values["version"] > created.values["version"]
        await service.delete("items", created.id, saved.edit_token, actor)
        assert (await service.list("items", AdminQuery(), actor)).total == 0
        async with async_sessionmaker(engine)() as session:
            from uuid import UUID

            item = await session.get(AdminPgItem, UUID(created.id))
            assert item is not None and item.deleted_at is not None
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(AdminPgItem.__table__.drop)
        await engine.dispose()
