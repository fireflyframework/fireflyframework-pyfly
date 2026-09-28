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
"""A relational repository whose derived or ``@query`` stubs no post-processor compiled fails the start (C138).

Without ``pyfly.data.relational.enabled`` the repository post-processor never ran: the derived and ``@query``
methods kept their ``...`` bodies and answered ``None`` (``exists_by_*`` a falsy ``None``, so a uniqueness check
"passed"), with or without a database. What the check looks at is those stubs: a post-processor registered by
hand (``context.register_post_processor(RepositoryBeanPostProcessor())``) compiles them too, and a repository it
did not bind to the context's transaction managers finds them when it is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.bean import bean
from pyfly.container.exceptions import BeanCreationException
from pyfly.container.stereotypes import configuration, repository
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.query import query
from pyfly.data.relational.auto_configuration import RepositoryWiringCheck
from pyfly.data.relational.sqlalchemy import Base, Repository
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor


class _WiredMember(Base):
    __tablename__ = "wp11_wired_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(80))


@repository
class WiredMemberRepository(Repository[_WiredMember, int]):
    async def exists_by_email(self, email: str) -> bool: ...


@repository
class QueriedMemberRepository(Repository[_WiredMember, int]):
    @query("SELECT count(*) FROM wp11_wired_member", native=True)
    async def count_everyone(self) -> int: ...


@repository
class PlainMemberRepository(Repository[_WiredMember, int]):
    """No derived or ``@query`` stub: only the inherited CRUD methods, which need no compiling."""


class _MemberQueries(Repository[_WiredMember, int]):
    """An application's intermediate base: the post-processor compiles its stubs for every repository that
    extends it (it walks the repository class's MRO)."""

    async def exists_by_email(self, email: str) -> bool: ...

    @query("SELECT count(*) FROM wp11_wired_member", native=True)
    async def count_everyone(self) -> int: ...


@repository
class InheritedMemberRepository(_MemberQueries):
    """Declares nothing itself: its derived and ``@query`` stubs are its base's."""


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


async def test_a_query_stub_without_the_relational_layer_fails_the_start(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(QueriedMemberRepository)
    with pytest.raises(BeanCreationException, match=r"count_everyone.*pyfly\.data\.relational\.enabled=true"):
        await context.start()
    await context.stop()


async def test_stubs_inherited_from_an_application_base_fail_the_start_when_uncompiled(tmp_path: Path) -> None:
    """The check looks where the post-processor does: the stubs a repository inherits from a base of the
    application's are compiled for it, so they must be compiled for the start to go on."""
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(InheritedMemberRepository)
    with pytest.raises(BeanCreationException, match=r"exists_by_email, count_everyone"):
        await context.start()
    await context.stop()


async def test_stubs_inherited_from_an_application_base_are_compiled_and_answer(tmp_path: Path) -> None:
    await _create_table(tmp_path)
    context = ApplicationContext(_config(tmp_path, enabled="true"))
    context.register_bean(InheritedMemberRepository)
    await context.start()
    try:
        members = context.get_bean(InheritedMemberRepository)
        await members.save(_WiredMember(email="a@example.com"))
        assert await members.exists_by_email("a@example.com") is True
        assert await members.count_everyone() == 1
    finally:
        await context.stop()


async def _create_table(tmp_path: Path) -> None:
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_WiredMember.metadata.create_all, tables=[_WiredMember.__table__])
    finally:
        await engine.dispose()


@pytest.mark.parametrize("enabled", ["true", "false"])
async def test_a_post_processor_registered_by_hand_wires_the_repository(tmp_path: Path, enabled: str) -> None:
    """The registration docs/modules/data-relational.md shows: with the relational layer enabled that processor
    replaces the auto-configured one and binds no transaction managers; the repository finds the context's when
    called, and its compiled queries run."""
    await _create_table(tmp_path)
    context = ApplicationContext(_config(tmp_path, enabled=enabled))
    context.register_post_processor(RepositoryBeanPostProcessor())
    context.register_bean(WiredMemberRepository)
    context.register_bean(QueriedMemberRepository)
    await context.start()
    try:
        members = context.get_bean(WiredMemberRepository)
        await members.save(_WiredMember(email="a@example.com"))
        assert await members.exists_by_email("a@example.com") is True
        assert await members.exists_by_email("b@example.com") is False
        assert await context.get_bean(QueriedMemberRepository).count_everyone() == 1
    finally:
        await context.stop()


async def test_a_repository_without_query_stubs_needs_no_compiling(tmp_path: Path) -> None:
    """Nothing of it answers None: its CRUD methods find the context's transaction managers when called."""
    await _create_table(tmp_path)
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(PlainMemberRepository)
    await context.start()
    try:
        members = context.get_bean(PlainMemberRepository)
        await members.save(_WiredMember(email="a@example.com"))
        assert await members.count() == 1
    finally:
        await context.stop()


class _LenientWiringCheck(RepositoryWiringCheck):
    """An application's own check, which lets every repository through."""

    def after_init(self, bean: Any, bean_name: str) -> Any:
        return bean


@configuration
class _ApplicationWiringCheck:
    @bean
    def lenient_wiring_check(self) -> RepositoryWiringCheck:
        return _LenientWiringCheck()


async def test_an_application_wiring_check_replaces_the_auto_configured_one(tmp_path: Path) -> None:
    """The check is an auto-configured bean like the other data beans: an application's own replaces it."""
    context = ApplicationContext(_config(tmp_path))
    context.register_bean(_ApplicationWiringCheck)
    context.register_bean(WiredMemberRepository)
    await context.start()
    try:
        assert [type(check) for check in context.get_beans_of_type(RepositoryWiringCheck)] == [_LenientWiringCheck]
    finally:
        await context.stop()
