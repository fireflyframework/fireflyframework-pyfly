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
"""Integration tests: MongoRepository against a real MongoDB (pymongo AsyncMongoClient/Beanie).

These tests exercise behaviour that mongomock cannot fully replicate:
  - Real pymongo async I/O with actual network round-trips
  - ``$regex`` filter queries executed by a live MongoDB
  - ``save_all`` + ``find_all_by_ids`` batch operations
  - Pagination total counts from a real aggregation pipeline
  - Beanie document insertion / retrieval with full BSON round-trip

Each test run uses a **unique database name** (uuid-based) so the session-scoped
``mongo_url`` fixture can be shared without cross-test contamination.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from beanie import init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.specification import MongoSpecification
from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.testing import requires_docker

# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class Article(BaseDocument):
    """Test document for MongoDB integration tests."""

    title: str
    author: str = ""
    published: bool = False
    score: float = Field(default=0.0)

    class Settings:
        name = "articles"
        use_state_management = True


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_mongo_repository_full(mongo_url: str) -> None:
    """Full MongoRepository smoke-test on real MongoDB: CRUD, pagination, spec, batch ops."""
    # Each run gets its own database to avoid cross-test contamination
    db_name = f"pyfly_it_{uuid.uuid4().hex[:12]}"
    client: AsyncMongoClient = AsyncMongoClient(mongo_url)  # type: ignore[type-arg]
    try:
        await init_beanie(database=client[db_name], document_models=[Article])
        repo: MongoRepository[Article, str] = MongoRepository(Article)

        # --- 1. save + find_by_id (BSON ObjectId round-trip) -----------------
        article = Article(title="Hello World", author="alice", published=True, score=8.5)
        saved = await repo.save(article)
        assert saved.id is not None

        found = await repo.find_by_id(saved.id)
        assert found is not None
        assert found.title == "Hello World"
        assert found.author == "alice"

        # --- 2. find_all + filter --------------------------------------------
        await repo.save(Article(title="Draft Post", author="bob", published=False))
        await repo.save(Article(title="Another Published", author="alice", published=True, score=7.0))

        all_articles = await repo.find_all()
        assert len(all_articles) == 3

        published = await repo.find_all(published=True)
        assert len(published) == 2
        assert all(a.published for a in published)

        # --- 3. find_paginated -----------------------------------------------
        # Insert more articles for pagination
        for i in range(10):
            await repo.save(Article(title=f"Bulk Article {i:02d}", author="system", score=float(i)))

        total_count = await repo.count()
        assert total_count >= 13  # 3 + 10

        page: Page[Article] = await repo.find_paginated(page=1, size=5)
        assert isinstance(page, Page)
        assert len(page.items) == 5
        assert page.total == total_count
        assert page.page == 1
        assert page.size == 5
        assert page.has_next is True

        page2: Page[Article] = await repo.find_paginated(page=2, size=5)
        assert len(page2.items) == 5
        assert page2.page == 2

        # Sorted pagination
        pageable = Pageable.of(1, 5, Sort(orders=(Order.desc("score"),)))
        sorted_page: Page[Article] = await repo.find_paginated(pageable=pageable)
        assert len(sorted_page.items) == 5
        # Highest score first
        scores = [a.score for a in sorted_page.items]
        assert scores == sorted(scores, reverse=True)

        # --- 4. MongoDB-specific $regex filter (real-Mongo path) -------------
        # mongomock does not handle all regex edge-cases accurately
        await repo.save(Article(title="Python Tutorial", author="carol"))
        await repo.save(Article(title="Python Advanced Tips", author="carol"))
        await repo.save(Article(title="JavaScript Basics", author="carol"))

        regex_spec: MongoSpecification[Article] = MongoSpecification(
            lambda root, f: {**f, "title": {"$regex": "^Python", "$options": "i"}}
        )
        python_articles = await repo.find_all_by_spec(regex_spec)
        assert len(python_articles) == 2
        assert all("Python" in a.title for a in python_articles)

        # --- 5. save_all + find_all_by_ids batch operations ------------------
        batch = [Article(title=f"Batch-{j}", author="batch_author", score=float(j)) for j in range(4)]
        saved_batch = await repo.save_all(batch)
        assert len(saved_batch) == 4
        assert all(a.id is not None for a in saved_batch)

        batch_ids = [a.id for a in saved_batch]
        fetched = await repo.find_all_by_ids(batch_ids)
        assert len(fetched) == 4
        assert {a.id for a in fetched} == set(batch_ids)

        # --- 6. delete -------------------------------------------------------
        to_delete = saved_batch[0]
        await repo.delete(to_delete.id)
        assert await repo.find_by_id(to_delete.id) is None

        # count reflects deletion
        new_count = await repo.count()
        # total_count=13 (3 explicit + 10 bulk); +3 regex articles (step 4); +4 batch (step 5); -1 deleted (step 6)
        assert new_count == total_count + 6  # +3 python/js articles + 4 batch - 1 deleted = net +6

        # --- 7. exists -------------------------------------------------------
        assert await repo.exists(saved_batch[1].id) is True
        # Use a valid ObjectId format that does not exist in the collection
        assert await repo.exists("000000000000000000000000") is False

        # --- 8. spec + pagination (find_all_by_spec_paged) -------------------
        author_spec: MongoSpecification[Article] = MongoSpecification(lambda root, f: {**f, "author": "batch_author"})
        spec_page: Page[Article] = await repo.find_all_by_spec_paged(author_spec, Pageable.of(1, 2))
        assert isinstance(spec_page, Page)
        assert spec_page.total == 3  # 4 saved - 1 deleted
        assert len(spec_page.items) == 2

    finally:
        # Drop the unique test database to keep MongoDB clean
        with contextlib.suppress(Exception):
            await client.drop_database(db_name)
        await client.close()
