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
"""Repository wiring after the Spring-parity rename.

Ensures the BeanPostProcessor still compiles derived-query stubs and that the
new real base methods ``exists_by_id`` / ``delete_by_id`` are NOT mistaken for
derived stubs (which would clobber the real implementations).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.repository import Repository


class Person(Base):
    __tablename__ = "wiring_people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(50))


class PersonRepo(Repository[Person, int]):
    async def find_by_email(self, email: str) -> list[Person]: ...  # derived stub
    async def exists_by_email(self, email: str) -> bool: ...  # derived stub
    async def delete_by_email(self, email: str) -> None: ...  # derived stub


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_base_spring_methods_are_not_clobbered(session):
    """The post-processor must leave real base methods intact."""
    repo = PersonRepo(session=session)
    RepositoryBeanPostProcessor().after_init(repo, "personRepo")

    saved = await repo.save(Person(name="Ann", email="ann@x.io"))
    # exists_by_id / delete_by_id are REAL base methods, not derived stubs
    assert await repo.exists_by_id(saved.id) is True


@pytest.mark.asyncio
async def test_derived_queries_are_wired(session):
    """find_by_*/exists_by_*/delete_by_* stubs get real implementations."""
    repo = PersonRepo(session=session)
    RepositoryBeanPostProcessor().after_init(repo, "personRepo")

    await repo.save(Person(name="Ann", email="ann@x.io"))
    await repo.save(Person(name="Bob", email="bob@x.io"))

    found = await repo.find_by_email("ann@x.io")
    assert [p.name for p in found] == ["Ann"]
    assert await repo.exists_by_email("bob@x.io") is True

    await repo.delete_by_email("ann@x.io")
    assert await repo.count() == 1
    assert await repo.exists_by_email("ann@x.io") is False
