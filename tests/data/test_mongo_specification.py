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
"""Tests for MongoSpecification — composable MongoDB query predicates."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.specification import MongoSpecification
from pyfly.data.pageable import Order, Pageable, Sort

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class User(BaseDocument):
    name: str
    role: str = "user"
    active: bool = True

    class Settings:
        name = "spec_test_users"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[User])
    yield
    client.close()


@pytest.fixture
async def seeded_repo() -> MongoRepository[User, str]:
    """Seed the database and return a repository."""
    users = [
        User(name="Alice", role="admin", active=True),
        User(name="Bob", role="user", active=True),
        User(name="Charlie", role="admin", active=False),
        User(name="Diana", role="user", active=False),
    ]
    for u in users:
        await u.save()
    return MongoRepository[User, str](User)


# ---------------------------------------------------------------------------
# Helper — get sorted names from find_all_by_spec
# ---------------------------------------------------------------------------


async def _names(repo: MongoRepository[User, str], spec: MongoSpecification[User]) -> list[str]:
    """Apply *spec* and return sorted list of matching user names."""
    results = await repo.find_all_by_spec(spec)
    return sorted(u.name for u in results)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMongoSpecificationSingle:
    """A single specification applies its predicate correctly."""

    @pytest.mark.asyncio
    async def test_filter_by_role(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        names = await _names(seeded_repo, admin_spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_filter_by_active(self, seeded_repo):
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        names = await _names(seeded_repo, active_spec)
        assert names == ["Alice", "Bob"]


class TestMongoSpecificationAnd:
    """AND (&) combination — both conditions must be satisfied."""

    @pytest.mark.asyncio
    async def test_and_combination(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        combined = admin_spec & active_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_and_three_specs(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        name_spec = MongoSpecification(lambda root, q: {"name": "Alice"})
        combined = admin_spec & active_spec & name_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Alice"]


class TestMongoSpecificationOr:
    """OR (|) combination — either condition may match."""

    @pytest.mark.asyncio
    async def test_or_combination(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        combined = admin_spec | active_spec
        names = await _names(seeded_repo, combined)
        # admin: Alice, Charlie  |  active: Alice, Bob  -> union: Alice, Bob, Charlie
        assert names == ["Alice", "Bob", "Charlie"]


class TestMongoSpecificationNot:
    """NOT (~) — negated condition."""

    @pytest.mark.asyncio
    async def test_not_active(self, seeded_repo):
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        inactive = ~active_spec
        names = await _names(seeded_repo, inactive)
        assert names == ["Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_not_admin(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        non_admin = ~admin_spec
        names = await _names(seeded_repo, non_admin)
        assert names == ["Bob", "Diana"]


class TestMongoSpecificationComplex:
    """Complex compositions: (spec1 & spec2) | spec3."""

    @pytest.mark.asyncio
    async def test_complex_composition(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        diana_spec = MongoSpecification(lambda root, q: {"name": "Diana"})
        # (admin AND active) OR Diana  -> Alice OR Diana
        combined = (admin_spec & active_spec) | diana_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Alice", "Diana"]

    @pytest.mark.asyncio
    async def test_not_combined_with_and(self, seeded_repo):
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        # NOT admin AND active -> non-admin active users -> Bob
        combined = ~admin_spec & active_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Bob"]


class TestMongoSpecificationNoop:
    """Empty / no-op specification."""

    @pytest.mark.asyncio
    async def test_noop_returns_all(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        names = await _names(seeded_repo, noop)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_noop_and_spec(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        combined = noop & admin_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_noop_or_spec(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        admin_spec = MongoSpecification(lambda root, q: {"role": "admin"})
        # noop has no filter, so OR falls through to admin_spec
        combined = noop | admin_spec
        names = await _names(seeded_repo, combined)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_not_noop(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        # NOT noop — no filter to negate, should return all
        negated = ~noop
        names = await _names(seeded_repo, negated)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]


# ---------------------------------------------------------------------------
# find_all_by_spec_paged integration
# ---------------------------------------------------------------------------


class TestFindAllBySpecPaged:
    """Integration tests for find_all_by_spec_paged."""

    @pytest.mark.asyncio
    async def test_paged_with_spec(self, seeded_repo):
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        pageable = Pageable(page=1, size=1)
        page = await seeded_repo.find_all_by_spec_paged(active_spec, pageable)
        assert page.total == 2
        assert page.size == 1
        assert len(page.items) == 1

    @pytest.mark.asyncio
    async def test_paged_noop_returns_all(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        pageable = Pageable(page=1, size=10)
        page = await seeded_repo.find_all_by_spec_paged(noop, pageable)
        assert page.total == 4
        assert len(page.items) == 4

    @pytest.mark.asyncio
    async def test_paged_with_sort(self, seeded_repo):
        active_spec = MongoSpecification(lambda root, q: {"active": True})
        pageable = Pageable(
            page=1,
            size=10,
            sort=Sort(orders=[Order(property="name", direction="asc")]),
        )
        page = await seeded_repo.find_all_by_spec_paged(active_spec, pageable)
        names = [u.name for u in page.items]
        assert names == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_paged_second_page(self, seeded_repo):
        noop = MongoSpecification(lambda root, q: {})
        pageable = Pageable(
            page=2,
            size=2,
            sort=Sort(orders=[Order(property="name", direction="asc")]),
        )
        page = await seeded_repo.find_all_by_spec_paged(noop, pageable)
        assert page.total == 4
        assert page.page == 2
        names = [u.name for u in page.items]
        assert names == ["Charlie", "Diana"]
