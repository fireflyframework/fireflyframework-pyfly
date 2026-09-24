"""Application-owned infrastructure; model administration borrows the same factory."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from catalog.controllers import CatalogController
from catalog.models import Product, ProductWrite
from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
from pyfly.admin.data.config import AdminDataProperties
from pyfly.admin.data.models import ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.admin.data.service import AdminDataService
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.http_basic_filter import HttpBasicAuthenticationFilter


def create_webapp(*, initialize_schema: bool = False):
    password = os.environ["WEBAPP_ADMIN_PASSWORD"]
    config = Config.from_file(Path(__file__).parents[2] / "pyfly.yaml")
    properties = config.bind(AdminDataProperties)
    if len(properties.edit_token_key) < 32:
        raise ValueError("Set WEBAPP_EDIT_TOKEN_KEY to a random secret of at least 32 characters")
    engine = create_async_engine(os.environ.get("WEBAPP_DATABASE_URL", "sqlite+aiosqlite:///catalog.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = SqlAlchemyAdminProvider(factory, edit_token_key=properties.edit_token_key)
    registry = AdminResourceRegistry()
    registry.register(
        ModelAdmin(
            "products",
            Product,
            label="Products",
            fields=("id", "name", "price", "updated_at"),
            editable_fields=("name", "price"),
            search_fields=("name",),
            filter_fields=("name",),
            operations=("list", "read", "create", "update", "delete"),
            create_schema=ProductWrite,
            update_schema=ProductWrite,
            provider=provider,
        )
    )
    context = ApplicationContext(config)
    context.container.register_instance(AdminResourceRegistry, registry)
    context.container.register_instance(AdminDataService, AdminDataService(registry, properties))
    encoder = BcryptPasswordEncoder()
    users = InMemoryUserDetailsService(
        UserDetails(username="admin", password_hash=encoder.hash(password), roles=["ADMIN"])
    )
    context.container.register_instance(HttpBasicAuthenticationFilter, HttpBasicAuthenticationFilter(users, encoder))
    context.register_bean(CatalogController)

    @asynccontextmanager
    async def lifespan(app):
        if initialize_schema:
            async with engine.begin() as connection:
                await connection.run_sync(Product.__table__.create, checkfirst=True)
        await context.start()
        try:
            yield
        finally:
            await context.stop()
            await engine.dispose()

    app = create_app(context=context, lifespan=lifespan, docs_enabled=False)
    app.state.catalog_factory = factory
    return app
