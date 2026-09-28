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
"""Advice on ``repository.*.*`` reaches the derived and ``@query`` methods (C113).

Every framework BeanPostProcessor had order 0, so their relative order was the alphabetical order
of the auto-configuration entry points: ``aop`` before ``relational``. The AOP post-processor wove
the stubs first, and the repository post-processor then replaced them on the instance with the
compiled queries, dropping the advice. A tracing, metrics, retry or security aspect silently
skipped the methods repositories use most. The repository post-processors now run first.

The context here discovers its auto-configurations from the entry points, as an application does
(registering ``RelationalAutoConfiguration`` by hand before ``start()`` hides the defect).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Integer, String, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from pyfly.aop.decorators import around, aspect  # noqa: E402
from pyfly.container import repository  # noqa: E402
from pyfly.container.ordering import get_order  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.query import query  # noqa: E402
from pyfly.data.relational.sqlalchemy.entity import Base  # noqa: E402
from pyfly.data.relational.sqlalchemy.repository import Repository  # noqa: E402


class _Book(Base):
    __tablename__ = "wp07_advice_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(50))


@repository
class _BookRepository(Repository[_Book, int]):
    async def find_by_title(self, title: str) -> list[_Book]: ...

    @query("SELECT b FROM _Book b WHERE b.title = :title")
    async def by_title(self, title: str) -> list[_Book]: ...


_ADVISED: list[str] = []


@aspect
class _Tracing:
    @around("repository.*.*")
    async def trace(self, join_point: Any) -> Any:
        _ADVISED.append(join_point.method_name)
        return await join_point.proceed()


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[str]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'books.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[_Book.__table__])
            await conn.execute(text("INSERT INTO wp07_advice_book (title) VALUES ('dune'), ('dune'), ('emma')"))
    finally:
        await engine.dispose()
    yield url


async def test_advice_wraps_the_compiled_derived_and_query_methods(database: str) -> None:
    _ADVISED.clear()
    ctx = ApplicationContext(
        Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": database, "ddl-auto": "none"}}}})
    )
    ctx.register_bean(_Tracing)
    ctx.register_bean(_BookRepository)
    await ctx.start()
    try:
        books = ctx.get_bean(_BookRepository)
        assert [book.title for book in await books.find_by_title("dune")] == ["dune", "dune"]
        assert [book.title for book in await books.by_title(title="dune")] == ["dune", "dune"]
        assert await books.count() == 3
    finally:
        await ctx.stop()

    assert _ADVISED == ["find_by_title", "by_title", "count"]


def test_repository_post_processors_run_before_the_aspect_post_processor() -> None:
    from pyfly.aop.post_processor import AspectBeanPostProcessor
    from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
    from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor

    aop = get_order(AspectBeanPostProcessor)
    assert get_order(RepositoryBeanPostProcessor) < aop
    assert get_order(MongoRepositoryBeanPostProcessor) < aop
