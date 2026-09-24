from decimal import Decimal

import pytest
from sqlalchemy import Numeric, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "admin_test_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    secret: Mapped[str] = mapped_column(default="private", deferred=True)


@pytest.fixture
async def admin(tmp_path):
    from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
    from pyfly.admin.data.models import AdminOperationContext, ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.admin.data.service import AdminDataService
    from pyfly.security.context import SecurityContext

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/data.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    resource = ModelAdmin(
        "products",
        Product,
        fields=("id", "name", "price"),
        editable_fields=("name", "price"),
        search_fields=("name",),
        operations=("list", "read", "create", "update", "delete"),
        provider=SqlAlchemyAdminProvider(factory, edit_token_key="test-key-32-bytes-of-entropy-value"),
    )
    registry = AdminResourceRegistry()
    registry.register(resource)
    service = AdminDataService(registry)
    actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
    try:
        yield service, actor, factory
    finally:
        await engine.dispose()


async def test_actual_crud_and_stale_edits(admin):
    from pyfly.kernel.exceptions import ConflictException

    service, actor, factory = admin
    created = await service.create("products", {"name": "Pen", "price": "2.50"}, actor)
    assert created.values["price"] == "2.50"
    assert "secret" not in created.values
    changed = await service.update("products", created.id, {"name": "Blue pen"}, created.edit_token, actor)
    with pytest.raises(ConflictException):
        await service.update("products", created.id, {"name": "Stale"}, created.edit_token, actor)
    async with factory() as session:
        assert (await session.get(Product, int(created.id))).name == "Blue pen"
    await service.delete("products", created.id, changed.edit_token, actor)
    async with factory() as session:
        assert await session.get(Product, int(created.id)) is None


async def test_reject_hidden_fields_and_rollback_constraints(admin):
    from pyfly.kernel.exceptions import ConflictException, ValidationException

    service, actor, factory = admin
    with pytest.raises(ValidationException):
        await service.create("products", {"name": "Pen", "secret": "overwrite"}, actor)
    await service.create("products", {"name": "Pen"}, actor)
    with pytest.raises(ConflictException):
        await service.create("products", {"name": "Pen"}, actor)
    created = await service.create("products", {"name": "Pencil"}, actor)
    assert created.values["name"] == "Pencil"
