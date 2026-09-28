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
"""Unified @transactional — ONE annotation for the relational and the document backend.

The relational arm runs on a real SQLite file database through the unit of work (a repository with no
session of its own joins the unit the service's legacy ``_session_factory`` resolves to). A service that also
exposes a legacy ``_motor_client`` must name its datasource (C143), and a ``_motor_client`` that is not pymongo's
``AsyncMongoClient`` is refused. The document arm, which needs a MongoDB replica set, is tested in
``tests/integration/test_unified_transactional_polyglot.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data import Isolation, transactional
from pyfly.data.document.mongodb import mongo_transactional
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import IllegalTransactionStateError, current_unit_of_work


def test_one_annotation_is_shared_across_backends() -> None:
    from pyfly.data.relational.sqlalchemy import transactional as relational_transactional

    assert transactional is relational_transactional
    assert mongo_transactional is transactional  # deprecated alias


# --------------------------------------------------------------------------- relational dispatch
class UnifiedRow(Base):
    __tablename__ = "unified_row"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


async def _relational_service(tmp_path: Path) -> tuple[Any, str, AsyncEngine]:
    """A service on a real SQLite file database, dispatched through its legacy ``_session_factory``."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'unified.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[UnifiedRow.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Svc:
        def __init__(self) -> None:
            self._session_factory = factory
            self.repo = Repository(UnifiedRow)

        @transactional()
        async def commit_path(self) -> str:
            await self.repo.save(UnifiedRow(name="committed"))
            return "ok"

        @transactional(isolation=Isolation.SERIALIZABLE)
        async def with_isolation(self) -> str:
            unit = current_unit_of_work()
            assert unit is not None
            level = await (await unit.resource.connection()).get_isolation_level()
            return str(level)

        @transactional()
        async def failing(self) -> str:
            await self.repo.save(UnifiedRow(name="rolled-back"))
            raise ValueError("boom")

    return Svc(), url, engine


async def _names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return [row[0] for row in (await conn.execute(text("SELECT name FROM unified_row ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_relational_commits_on_success(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        assert await svc.commit_path() == "ok"
        assert await _names(engine) == ["committed"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_isolation_reaches_the_connection(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        # The audit flagged this as unverified behind a mock (F6): read the level back from the connection.
        assert await svc.with_isolation() == "SERIALIZABLE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_rolls_back_on_exception(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="boom"):
            await svc.failing()
        assert await _names(engine) == []
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_polyglot_service_must_name_its_datasource(tmp_path: Path) -> None:
    """C143: exposing both factories used to run only the relational arm, so Mongo writes committed."""
    svc, _url, engine = await _relational_service(tmp_path)
    svc._motor_client = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
    try:
        with pytest.raises(IllegalTransactionStateError, match="both"):
            await svc.commit_path()
        assert await _names(engine) == []
    finally:
        await engine.dispose()
        await svc._motor_client.close()


@pytest.mark.asyncio
async def test_no_manager_anywhere_raises() -> None:
    class NoClient:
        @transactional()
        async def work(self, *, session: Any = None) -> None: ...

    # Neither factory attribute and no running context: an IllegalTransactionStateError (a RuntimeError).
    with pytest.raises(RuntimeError, match="No transaction manager"):
        await NoClient().work()


@pytest.mark.asyncio
async def test_a_motor_client_that_is_not_pymongos_is_refused() -> None:
    class OtherClient:
        pass

    class Svc:
        def __init__(self) -> None:
            self._motor_client = OtherClient()

        @transactional()
        async def work(self) -> None: ...

    with pytest.raises(IllegalTransactionStateError, match="_motor_client"):
        await Svc().work()
