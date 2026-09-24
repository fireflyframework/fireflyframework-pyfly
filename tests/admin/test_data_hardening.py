import asyncio
from dataclasses import replace

import pytest

from pyfly.admin.data.models import AdminQuery
from pyfly.kernel.exceptions import (
    ConflictException,
    ForbiddenException,
    ResourceNotFoundException,
    ValidationException,
)
from tests.admin.test_data_sqlalchemy import Product
from tests.admin.test_data_sqlalchemy import admin as _admin_fixture

admin = _admin_fixture


async def test_concurrent_edits_exactly_one_commits(admin):
    service, actor, factory = admin
    record = await service.create("products", {"name": "Original"}, actor)
    results = await asyncio.gather(
        *(
            service.update("products", record.id, {"name": name}, record.edit_token, actor)
            for name in ("First", "Second")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictException) for result in results) == 1
    async with factory() as session:
        assert (await session.get(Product, int(record.id))).name in ("First", "Second")


async def test_scope_query_allowlists_and_authorization(admin):
    service, actor, factory = admin
    record = await service.create("products", {"name": "100% literal"}, actor)
    await service.create("products", {"name": "1000 unrelated"}, actor)
    result = await service.list("products", AdminQuery(search="%"), actor)
    assert result.total == 1 and result.items[0].id == record.id
    for query in (AdminQuery(size=101), AdminQuery(filters={"secret": "private"}), AdminQuery(sort=("secret",))):
        with pytest.raises(ValidationException):
            await service.list("products", query, actor)
    resource = service.registry.get("products")
    resource.scope = lambda context: {"id": -1}
    assert (await service.list("products", AdminQuery(), actor)).total == 0
    with pytest.raises(ResourceNotFoundException):
        await service.get("products", record.id, actor)
    with pytest.raises(ResourceNotFoundException):
        await service.delete("products", record.id, record.edit_token, actor)
    from pyfly.security.context import SecurityContext

    with pytest.raises(ForbiddenException):
        await service.get(
            "products", record.id, replace(actor, security=SecurityContext(user_id="viewer", roles=["VIEWER"]))
        )


async def test_invalid_edit_token_is_conflict_not_server_error(admin):
    service, actor, _ = admin
    record = await service.create("products", {"name": "Token"}, actor)
    with pytest.raises(ConflictException):
        await service.delete("products", record.id, "é", actor)


async def test_audit_never_logs_submitted_values(admin, caplog):
    service, actor, _ = admin
    with caplog.at_level("INFO", logger="pyfly.admin.data.audit"):
        await service.create("products", {"name": "SENSITIVE_VALUE"}, actor)
    assert "result=success" in caplog.text and "actor=admin" in caplog.text
    assert "SENSITIVE_VALUE" not in caplog.text


async def test_custom_field_adapter_preserves_model_type(admin):
    from decimal import Decimal

    from pyfly.admin.data.models import AdminFieldAdapter

    service, actor, factory = admin
    resource = service.registry.get("products")
    resource.field_adapters = {
        "price": AdminFieldAdapter("integer", lambda v: int(v * 100), lambda v: Decimal(v) / 100)
    }
    record = await service.create("products", {"name": "Adapter", "price": 250}, actor)
    assert record.values["price"] == 250
    async with factory() as session:
        assert (await session.get(Product, int(record.id))).price == Decimal("2.50")


async def test_adapter_for_sqlalchemy_type_without_python_type(tmp_path):
    from sqlalchemy import String, TypeDecorator
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
    from pyfly.admin.data.models import AdminFieldAdapter, AdminOperationContext, ModelAdmin
    from pyfly.security.context import SecurityContext

    class CodeType(TypeDecorator):
        impl = String
        cache_ok = True

    class Base(DeclarativeBase):
        pass

    class Item(Base):
        __tablename__ = "custom_items"
        id: Mapped[int] = mapped_column(primary_key=True)
        code: Mapped[str] = mapped_column(CodeType())

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/adapter.db")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        provider = SqlAlchemyAdminProvider(
            async_sessionmaker(engine), edit_token_key="custom-field-key-of-at-least-32-bytes"
        )
        resource = ModelAdmin(
            "items",
            Item,
            fields=("id", "code"),
            editable_fields=("code",),
            operations=("create", "update"),
            field_adapters={"code": AdminFieldAdapter("string", str, str)},
        )
        actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
        created = await provider.create(resource, {"code": "FIRST"}, actor)
        changed = await provider.update(resource, created.id, {"code": "SECOND"}, created.edit_token, actor)
        assert changed.values["code"] == "SECOND"
    finally:
        await engine.dispose()


async def test_policy_hook_cannot_expand_readonly_operations(admin):
    from pyfly.admin.data.models import ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.admin.data.service import AdminDataService

    service, actor, _ = admin
    created = await service.create("products", {"name": "Read only"}, actor)

    class ReadOnlyAdmin(ModelAdmin):
        def has_permission(self, operation, context, record=None):
            return context.security.has_role("ADMIN")

    registration = ReadOnlyAdmin(
        "products",
        Product,
        fields=("id", "name"),
        editable_fields=("name",),
        provider=service.registry.get("products").provider,
    )
    registry = AdminResourceRegistry()
    registry.register(registration)
    limited = AdminDataService(registry)
    with pytest.raises(ForbiddenException):
        await limited.delete("products", created.id, created.edit_token, actor)
    with pytest.raises(ForbiddenException):
        await limited.create("products", {"name": "Should fail"}, actor)


def test_large_integer_wire_values_are_lossless():
    from pyfly.admin.data.identifiers import json_value

    assert json_value(9007199254740993) == "9007199254740993"
    assert json_value(-9007199254740993) == "-9007199254740993"
    assert json_value(42) == 42
    assert json_value(True) is True


async def test_hidden_deferred_column_changes_invalidate_token(admin):
    from sqlalchemy import update

    service, actor, factory = admin
    record = await service.create("products", {"name": "Hidden change"}, actor)
    async with factory() as session, session.begin():
        await session.execute(update(Product).where(Product.id == int(record.id)).values(secret="changed"))
    with pytest.raises(ConflictException):
        await service.update("products", record.id, {"name": "Outdated edit"}, record.edit_token, actor)
    assert "secret" not in (await service.get("products", record.id, actor)).values


async def test_composite_keys_json_enums_and_scoped_creation(tmp_path):
    from enum import Enum
    from uuid import UUID, uuid4

    from sqlalchemy import JSON
    from sqlalchemy import Enum as SqlEnum
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
    from pyfly.admin.data.models import AdminOperationContext, ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.admin.data.service import AdminDataService
    from pyfly.security.context import SecurityContext

    class Color(Enum):
        RED = "red"
        BLUE = "blue"

    class Base(DeclarativeBase):
        pass

    class Item(Base):
        __tablename__ = "composite_items"
        tenant: Mapped[str] = mapped_column(primary_key=True)
        code: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
        color: Mapped[Color] = mapped_column(SqlEnum(Color), default=Color.RED)
        payload: Mapped[dict] = mapped_column(JSON, default=dict)

    class TenantAdmin(ModelAdmin):
        def scope(self, context):
            return {"tenant": context.security.user_id}

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/composite.db")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        registry = AdminResourceRegistry()
        registry.register(
            TenantAdmin(
                "items",
                Item,
                fields=("tenant", "code", "color", "payload"),
                editable_fields=("color", "payload"),
                operations=("list", "read", "create", "update", "delete"),
                provider=SqlAlchemyAdminProvider(
                    async_sessionmaker(engine), edit_token_key="composite-key-test-secret-at-least32"
                ),
            )
        )
        service = AdminDataService(registry)
        actor = AdminOperationContext(SecurityContext(user_id="tenant-a", roles=["ADMIN"]))
        created = await service.create("items", {"color": "blue", "payload": {"number": 42, "nullable": None}}, actor)
        assert created.values["tenant"] == "tenant-a" and created.values["color"] == "blue"
        assert (await service.get("items", created.id, actor)).values["payload"] == {"number": 42, "nullable": None}
        other = replace(actor, security=SecurityContext(user_id="tenant-b", roles=["ADMIN"]))
        assert (await service.list("items", AdminQuery(), other)).total == 0
        with pytest.raises(ResourceNotFoundException):
            await service.get("items", created.id, other)
        with pytest.raises(ValidationException):
            await service.get("items", "malformed-identifier", actor)
        changed = await service.update("items", created.id, {"color": "red"}, created.edit_token, actor)
        await service.delete("items", created.id, changed.edit_token, actor)
        assert (await service.list("items", AdminQuery(), actor)).total == 0
    finally:
        await engine.dispose()
