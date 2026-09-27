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
"""A relational repository in a context whose relational data layer is off fails the start (C138).

Without ``pyfly.data.relational.enabled`` the repository post-processor never ran: the derived and ``@query``
methods kept their ``...`` bodies and answered ``None`` (``exists_by_*`` a falsy ``None``, so a uniqueness check
"passed"), with or without a database, while the inherited CRUD methods failed only when first called.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.exceptions import BeanCreationException
from pyfly.container.stereotypes import repository
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.sqlalchemy import Base, Repository


class _WiredMember(Base):
    __tablename__ = "wp11_wired_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(80))


@repository
class WiredMemberRepository(Repository[_WiredMember, int]):
    async def exists_by_email(self, email: str) -> bool: ...


def _config(tmp_path: Path, **relational: str) -> Config:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    return Config({"pyfly": {"data": {"relational": {"url": url, **relational}}}})


async def test_a_repository_without_the_relational_layer_fails_the_start(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(WiredMemberRepository)
    with pytest.raises(BeanCreationException, match=r"pyfly\.data\.relational\.enabled=true"):
        await context.start()
    await context.stop()


async def test_a_repository_of_the_relational_layer_starts_and_answers(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path, enabled="true"))
    context.register_bean(WiredMemberRepository)
    await context.start()
    try:
        members = context.get_bean(WiredMemberRepository)
        await members.save(_WiredMember(email="a@example.com"))
        assert await members.exists_by_email("a@example.com") is True
        assert await members.exists_by_email("b@example.com") is False
    finally:
        await context.stop()


async def test_a_repository_with_its_own_session_is_left_alone(tmp_path: Path) -> None:
    """Manual mode: the caller hands the repository a session and owns it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'manual.db'}")
    session = async_sessionmaker(engine)()
    context = ApplicationContext(Config({}))
    context.container.register_instance(WiredMemberRepository, WiredMemberRepository(_WiredMember, session))
    try:
        await context.start()
        assert context.get_bean(WiredMemberRepository)._session is session
    finally:
        await context.stop()
        await session.close()
        await engine.dispose()
