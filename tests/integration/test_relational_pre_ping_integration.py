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
"""``pool.pre-ping: true`` on MySQL and MariaDB, through the application path (C089).

``pool.pre-ping`` is the documented remedy for a server that drops idle connections (``wait_timeout``).
With SQLAlchemy 2.0.49 and PyMySQL >= 1.2 installed, every checkout of a reused pooled connection raised
``TypeError: AsyncAdapt_..._connection.ping() missing 1 required positional argument: 'reconnect'``
on both async drivers, so every other request failed. SQLAlchemy 2.0.50 fixed the adapters, and the
``data-relational`` extra now requires it.

This runs the path an operator runs: an ``ApplicationContext`` with ``RelationalAutoConfiguration``,
``pool.pre-ping`` on, and 40 sequential ``@transactional`` saves through a ``@repository``, on MySQL 8
and MariaDB 11, through both drivers the docs name (asyncmy from the ``mysql`` extra, and aiomysql).
PyMySQL is installed in these lanes (aiomysql depends on it), which is the condition that broke asyncmy.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transactional import transactional
from tests.support.backend_matrix import MARIADB, MYSQL, MYSQL_DRIVERS, RelationalBackend

_CALLS = 40


class PrePingItem(Base):
    __tablename__ = "pre_ping_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class PrePingItemRepository(Repository[PrePingItem, int]):
    pass


@service
class PrePingItemService:
    def __init__(self, repo: PrePingItemRepository, factory: async_sessionmaker[AsyncSession]) -> None:
        self.repo = repo
        self._session_factory = factory

    @transactional
    async def create(self, name: str) -> None:
        await self.repo.save(PrePingItem(name=name))


@pytest.mark.backends(MYSQL, MARIADB)
@pytest.mark.parametrize("driver", MYSQL_DRIVERS)
async def test_every_transactional_call_succeeds_with_pre_ping_on(
    relational_backend: RelationalBackend, driver: str
) -> None:
    backend = relational_backend.with_driver(driver)
    await backend.create_tables(PrePingItem)
    context = ApplicationContext(backend.config())
    for bean in (RelationalAutoConfiguration, PrePingItemRepository, PrePingItemService):
        context.register_bean(bean)
    await context.start()
    outcomes: Counter[str] = Counter()
    try:
        # The key reached the pool (a vacuous pass is what this guards against).
        assert context.get_bean(AsyncEngine).sync_engine.pool._pre_ping is True
        items = context.get_bean(PrePingItemService)
        for i in range(_CALLS):
            try:
                await items.create(f"item-{i}")
                outcomes["ok"] += 1
            except Exception as exc:  # noqa: BLE001 — the failure mode under test is an arbitrary exception type
                outcomes[f"{type(exc).__name__}: {exc}"] += 1
    finally:
        await context.stop()

    assert outcomes == {"ok": _CALLS}
    engine = backend.create_engine()
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(PrePingItem))).scalar_one() == _CALLS
