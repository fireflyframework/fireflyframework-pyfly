from importlib import import_module

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pyfly.admin.data.models import ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.admin.data.wiring import build_data_routes
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.named_datasources import NamedDataSources
from tests.admin.test_data_sqlalchemy import Product


def test_borrowed_provider_rebuilt_without_mutating_registration(tmp_path):
    first = async_sessionmaker(create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/one.db"))
    second = async_sessionmaker(create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/two.db"))
    registry = AdminResourceRegistry()
    resource = ModelAdmin("products", Product, datasource="reporting", fields=("id", "name"))
    registry.register(resource)
    context = ApplicationContext(Config({"pyfly": {"admin": {"data": {"enabled": True}}}}))
    context.container.register_instance(AdminResourceRegistry, registry)
    context.container.register_instance(NamedDataSources, NamedDataSources({"reporting": first}))
    hooks = []
    routes = build_data_routes(context, "/console", hooks)
    hooks[0]()
    route_owner = routes[0].endpoint.__self__
    initial = route_owner.service.registry.get("products").provider
    assert initial._factory is first
    assert resource.provider is None
    context.container.register_instance(NamedDataSources, NamedDataSources({"reporting": second}))
    hooks[0]()
    assert route_owner.service.registry.get("products").provider._factory is second
    assert route_owner.service.registry.get("products").provider is not initial


@pytest.mark.parametrize("adapter", ["starlette", "fastapi"])
def test_disabled_data_has_no_api_routes(adapter):
    context = ApplicationContext(Config({"pyfly": {"admin": {"enabled": True}}}))
    app = import_module(f"pyfly.web.adapters.{adapter}.app").create_app(context=context)
    assert not any("/api/data/" in getattr(route, "path", "") for route in app.routes)
