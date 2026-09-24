"""A real, isolated SQL admin server for browser integration tests."""

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
from pyfly.admin.data.models import ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.http_basic_filter import HttpBasicAuthenticationFilter
from tests.admin.test_data_sqlalchemy import Base, Product

engine = create_async_engine(os.environ["PYFLY_BROWSER_DATABASE"])
registry = AdminResourceRegistry()
registry.register(
    ModelAdmin(
        "products",
        Product,
        label="Products",
        fields=("id", "name", "price"),
        editable_fields=("name", "price"),
        search_fields=("name",),
        operations=("list", "read", "create", "update", "delete"),
        provider=SqlAlchemyAdminProvider(
            async_sessionmaker(engine, expire_on_commit=False), edit_token_key="browser-tests-secret-key-long-enough"
        ),
    )
)
ctx = ApplicationContext(
    Config(
        {
            "pyfly": {
                "admin": {
                    "path": os.environ.get("PYFLY_BROWSER_ADMIN_PATH", "/admin"),
                    "enabled": True,
                    "data": {"enabled": True, "edit-token-key": "browser-tests-secret-key-long-enough"},
                },
                "security": {"csrf": {"cookie-secure": False}},
            }
        }
    )
)
ctx.container.register_instance(AdminResourceRegistry, registry)
encoder = BcryptPasswordEncoder(rounds=4)
users = InMemoryUserDetailsService(
    UserDetails(username="admin", password_hash=encoder.hash("browser-test-password"), roles=["ADMIN"])
)
ctx.container.register_instance(HttpBasicAuthenticationFilter, HttpBasicAuthenticationFilter(users, encoder))


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await ctx.start()
    try:
        yield
    finally:
        await ctx.stop()
        await engine.dispose()


app = create_app(context=ctx, lifespan=lifespan, docs_enabled=False)
