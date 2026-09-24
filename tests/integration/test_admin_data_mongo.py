from uuid import uuid4

import pytest
from beanie import init_beanie
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
