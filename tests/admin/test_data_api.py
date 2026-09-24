from importlib import import_module

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security.jwt import JWTService
from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter
from tests.admin.test_data_sqlalchemy import admin as _admin_fixture

admin = _admin_fixture


@pytest.fixture(params=["starlette", "fastapi"])
async def api(request, admin):
    from pyfly.admin.data.registry import AdminResourceRegistry

    service, actor, factory = admin
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "admin": {
                        "enabled": True,
                        "require-auth": False,
                        "data": {"enabled": True, "edit-token-key": "test-key-32-bytes-of-entropy-value"},
                    },
                    "security": {"csrf": {"cookie-secure": False}},
                }
            }
        )
    )
    ctx.container.register_instance(AdminResourceRegistry, service.registry)
    jwt = JWTService("test-jwt-signing-key-long-enough-for-hs256")
    ctx.container.register_instance(SecurityFilter, SecurityFilter(jwt))
    await ctx.start()
    app = import_module(f"pyfly.web.adapters.{request.param}.app").create_app(context=ctx, docs_enabled=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, jwt
    finally:
        await ctx.stop()


def test_admin_data_auth_csrf_and_crud(api):
    client, jwt = api
    base = "/admin/api/data"
    assert client.get(base + "/sources").status_code == 401
    client.headers["Authorization"] = "Bearer " + jwt.encode({"sub": "admin", "roles": ["ADMIN"]})
    response = client.get(base + "/sources")
    assert response.status_code == 200, response.text
    records = base + "/resources/products/records"
    assert client.post(records, json={"values": {"name": "Forgery"}}).status_code == 403
    client.headers["X-XSRF-TOKEN"] = client.cookies["XSRF-TOKEN"]
    response = client.post(records, json={"values": {"name": "Pen"}})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["values"]["name"] == "Pen"
    response = client.patch(
        records + "/" + record["id"], json={"values": {"name": "Pencil"}, "editToken": record["edit_token"]}
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    response = client.delete(records + "/" + record["id"], headers={"If-Match": changed["edit_token"]})
    assert response.status_code == 204


def test_forged_bearer_does_not_authorize(api):
    client, jwt = api
    client.headers["Authorization"] = "Bearer forged"
    assert (
        client.post("/admin/api/data/resources/products/records", json={"values": {"name": "Bad"}}).status_code == 401
    )


@pytest.mark.parametrize("identifier", ["schema", "folder/schema"])
async def test_string_ids_are_not_confused_with_schema_routes(api, identifier):
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    from pyfly.admin.data.models import ModelAdmin

    client, jwt = api

    class Base(DeclarativeBase):
        pass

    class Label(Base):
        __tablename__ = "admin_string_labels"
        id: Mapped[str] = mapped_column(primary_key=True)
        label: Mapped[str]

    owner = next(
        route.endpoint.__self__ for route in client.app.routes if getattr(route, "name", "") == "admin-data-sources"
    )
    provider = owner.service.registry.get("products").provider
    async with provider._factory() as session:
        connection = await session.connection()
        await connection.run_sync(Base.metadata.create_all)
        session.add(Label(id=identifier, label="Named record"))
        await session.commit()
    owner.service.registry.register(ModelAdmin("labels", Label, fields=("id", "label"), provider=provider))
    client.headers["Authorization"] = "Bearer " + jwt.encode({"sub": "admin", "roles": ["ADMIN"]})
    from urllib.parse import quote

    response = client.get("/admin/api/data/resources/labels/records/" + quote(identifier, safe=""))
    assert response.status_code == 200, response.text
    assert response.json()["values"]["label"] == "Named record"
