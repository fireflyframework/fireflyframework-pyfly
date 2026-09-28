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
"""The data admin's Beanie provider on a real MongoDB: CRUD with edit tokens, any id type, and the round trips
each write costs (C090)."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import uuid4

import pytest
from beanie import init_beanie
from bson import Binary, ObjectId
from pymongo import AsyncMongoClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.kernel.exceptions import ConflictException
from pyfly.security.context import SecurityContext


class AdminArticle(BaseDocument):
    title: str
    secret: str = "private"

    class Settings:
        name = "articles"


async def test_admin_mongo_real_crud(mongo_url):
    from pyfly.admin.data.adapters.beanie import BeanieAdminProvider
    from pyfly.admin.data.models import AdminOperationContext, ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.admin.data.service import AdminDataService

    client = AsyncMongoClient(mongo_url)
    database = client[f"pyfly_admin_it_{uuid4().hex}"]
    try:
        await init_beanie(database=database, document_models=[AdminArticle])
        registry = AdminResourceRegistry()
        registry.register(
            ModelAdmin(
                "articles",
                AdminArticle,
                fields=("id", "title"),
                editable_fields=("title",),
                operations=("list", "read", "create", "update", "delete"),
                datasource="document",
                provider=BeanieAdminProvider(edit_token_key="test-key-of-at-least-32-characters"),
            )
        )
        service = AdminDataService(registry)
        actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
        created = await service.create("articles", {"title": "First"}, actor)
        assert created.values["title"] == "First"
        assert "secret" not in created.values
        changed = await service.update("articles", created.id, {"title": "Second"}, created.edit_token, actor)
        assert (await AdminArticle.get(created.id)).title == "Second"
        with pytest.raises(ConflictException):
            await service.delete("articles", created.id, created.edit_token, actor)
        await service.delete("articles", created.id, changed.edit_token, actor)
        assert await AdminArticle.get(created.id) is None
    finally:
        await client.drop_database(database.name)
        await client.close()


class AdminLabel(BaseDocument):
    id: str | None = None  # type: ignore[assignment]
    name: str

    class Settings:
        name = "labels"


class AdminTicket(BaseDocument):
    id: uuid.UUID | None = None  # type: ignore[assignment]
    subject: str

    class Settings:
        name = "tickets"


def _service(model: type, resource: str, field: str) -> Any:
    from pyfly.admin.data.adapters.beanie import BeanieAdminProvider
    from pyfly.admin.data.models import ModelAdmin
    from pyfly.admin.data.registry import AdminResourceRegistry
    from pyfly.admin.data.service import AdminDataService

    registry = AdminResourceRegistry()
    registry.register(
        ModelAdmin(
            resource,
            model,
            fields=("id", field),
            editable_fields=(field,),
            operations=("list", "read", "create", "update", "delete"),
            datasource="document",
            provider=BeanieAdminProvider(edit_token_key="test-key-of-at-least-32-characters"),
        )
    )
    return AdminDataService(registry)


@pytest.mark.parametrize(
    ("model", "resource", "field"), [(AdminLabel, "labels", "name"), (AdminTicket, "tickets", "subject")]
)
async def test_admin_crud_with_string_and_uuid_ids(mongo_rs_url: str, model: type, resource: str, field: str) -> None:
    """The provider parsed every id as an ObjectId: a document with another id type could not be read or written."""
    from pyfly.admin.data.models import AdminOperationContext
    from tests.support.mongo import beanie_database

    async with beanie_database(mongo_rs_url, [model]) as db:
        service = _service(model, resource, field)
        actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
        created = await service.create(resource, {field: "first"}, actor)
        stored = await db.database[resource].find_one({})
        identity = stored["_id"]
        if isinstance(identity, Binary):  # this client has no UUID representation: a UUID reads as BSON binary
            identity = identity.as_uuid()
        assert str(identity) == created.id
        assert type(identity) is not ObjectId  # the document's own id type, made on the client
        fetched = await service.get(resource, created.id, actor)
        assert fetched.values[field] == "first"
        changed = await service.update(resource, created.id, {field: "second"}, created.edit_token, actor)
        assert changed.values[field] == "second"
        await service.delete(resource, created.id, changed.edit_token, actor)
        assert await db.database[resource].count_documents({}) == 0


async def test_admin_writes_cost_one_round_trip_each(mongo_rs_url: str) -> None:
    """Creating reads nothing back; updating and deleting read the document and write it conditionally, one
    command each, instead of writing and reading it again."""
    from pyfly.admin.data.models import AdminOperationContext
    from tests.support.mongo import beanie_database

    async with beanie_database(mongo_rs_url, [AdminArticle]) as db:
        service = _service(AdminArticle, "articles", "title")
        actor = AdminOperationContext(SecurityContext(user_id="admin", roles=["ADMIN"]))
        db.log.clear()
        created = await service.create("articles", {"title": "First"}, actor)
        assert db.log.names() == ["insert"]
        assert created.values["title"] == "First"
        db.log.clear()
        changed = await service.update("articles", created.id, {"title": "Second"}, created.edit_token, actor)
        assert db.log.names() == ["find", "findAndModify"]
        assert changed.values["title"] == "Second"
        with pytest.raises(ConflictException):
            await service.update("articles", created.id, {"title": "Stale"}, created.edit_token, actor)
        db.log.clear()
        await service.delete("articles", created.id, changed.edit_token, actor)
        assert db.log.names() == ["find", "findAndModify"]
        assert await db.database["articles"].count_documents({}) == 0
