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
"""Units of work on an in-memory SQLite database (``StaticPool``: one connection every session shares).

Two units cannot overlap on that connection: the second one used to fail with SQLite's "cannot start a
transaction within a transaction", or to share the first one's transaction. It now fails at once with
``IllegalTransactionStateError`` saying why, and the first unit is unaffected. A unit cancelled between
two statements rolls back instead of discarding the connection, which would drop the whole database.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import anyio
import pytest
from sqlalchemy import Identity, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import IllegalTransactionStateError


class MemItem(Base):
    __tablename__ = "mem_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class MemItemRepository(Repository[MemItem, int]):
    pass


@service
class MemService:
    def __init__(self, items: MemItemRepository) -> None:
        self.items = items

    @transactional
    async def place_then_wait(self, name: str, seconds: float) -> None:
        await self.items.save(MemItem(name=name))
        await asyncio.sleep(seconds)


@pytest.fixture
async def memory() -> AsyncIterator[ApplicationContext]:
    relational = {"enabled": "true", "url": "sqlite+aiosqlite:///:memory:", "ddl-auto": "create"}
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": relational}}}))
    for bean in (RelationalAutoConfiguration, MemItemRepository, MemService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield ctx
    finally:
        await ctx.stop()


async def _names(ctx: ApplicationContext) -> list[str]:
    return [item.name for item in await ctx.get_bean(MemItemRepository).find_all()]


async def test_a_unit_that_would_share_the_connection_fails_at_once(memory: ApplicationContext) -> None:
    service_ = memory.get_bean(MemService)
    first, second = await asyncio.gather(
        service_.place_then_wait("first", 0.05),
        service_.place_then_wait("second", 0),
        return_exceptions=True,
    )
    assert first is None
    assert isinstance(second, IllegalTransactionStateError)
    assert "in-memory SQLite database" in str(second)
    assert await _names(memory) == ["first"]


async def test_a_repository_call_while_a_stream_holds_the_connection_fails_at_once(memory: ApplicationContext) -> None:
    items = memory.get_bean(MemItemRepository)
    await items.save_all([MemItem(name="a"), MemItem(name="b")])
    seen = []
    with pytest.raises(IllegalTransactionStateError, match="stream_all"):
        async with contextlib.aclosing(items.stream_all()) as stream:
            async for item in stream:
                seen.append(item.name)
                await items.count()
    assert seen == ["a"]
    assert await items.count() == 2  # the closed stream gave the connection back


async def test_a_cancelled_unit_keeps_the_database(memory: ApplicationContext) -> None:
    items = memory.get_bean(MemItemRepository)
    await items.save(MemItem(name="kept"))
    with anyio.move_on_after(0.05) as scope:
        await memory.get_bean(MemService).place_then_wait("cancelled", 5)
    assert scope.cancelled_caught
    assert await _names(memory) == ["kept"]  # rolled back, and the database is still there
    await items.save(MemItem(name="next"))
    assert await _names(memory) == ["kept", "next"]
