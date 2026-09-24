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
"""The relational auto-configuration hands out ONE ``AsyncSession`` PER INJECTION.

``async_session`` used to be a singleton: every repository, every user bean and the engine
lifecycle shared one SQLAlchemy session, which is one transaction, one identity map and one
connection-local state (``SET LOCAL``, a tenant GUC, a search_path) for the whole process.
Anything multi-tenant or concurrent had to refuse the bean and build its own sessions. The
bean is now transient — the factory is the unit of sharing, the session is the unit of work —
and a user ``@bean`` that wants the framework's ``async_sessionmaker`` is deferred until the
auto-configuration has registered it (see ``tests/context/test_bean_method_after_auto_configuration.py``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from pyfly.container.bean import bean  # noqa: E402
from pyfly.container.stereotypes import configuration, service  # noqa: E402
from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.auto_configuration import EngineLifecycle, RelationalAutoConfiguration  # noqa: E402


def _relational_config() -> Config:
    return Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": "true",
                        "url": "sqlite+aiosqlite:///:memory:",
                        "ddl-auto": "none",
                    }
                }
            }
        }
    )


class UnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory


class TestTransientSession:
    async def test_two_injections_receive_two_sessions(self) -> None:
        @service
        class FirstConsumer:
            def __init__(self, session: AsyncSession) -> None:
                self.session = session

        @service
        class SecondConsumer:
            def __init__(self, session: AsyncSession) -> None:
                self.session = session

        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(RelationalAutoConfiguration)
        ctx.register_bean(FirstConsumer)
        ctx.register_bean(SecondConsumer)
        await ctx.start()
        try:
            first = ctx.get_bean(FirstConsumer).session
            second = ctx.get_bean(SecondConsumer).session
            assert isinstance(first, AsyncSession)
            assert first is not second
            assert ctx.get_bean(AsyncSession) is not first
        finally:
            await ctx.stop()

    async def test_the_engine_lifecycle_still_closes_its_own_session(self) -> None:
        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(RelationalAutoConfiguration)
        await ctx.start()
        lifecycle = ctx.get_bean(EngineLifecycle)
        await ctx.stop()
        # A transient session was handed to the lifecycle at construction and closed at stop —
        # closing twice is what would break, and it does not.
        await lifecycle.stop()

    async def test_a_user_bean_can_take_the_framework_session_factory(self) -> None:
        """The real-world case the deferral exists for: a per-unit-of-work session wrapper."""

        @configuration
        class UserConfiguration:
            @bean
            def unit_of_work(self, factory: async_sessionmaker[AsyncSession]) -> UnitOfWork:
                return UnitOfWork(factory)

        ctx = ApplicationContext(_relational_config())
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(RelationalAutoConfiguration)
        await ctx.start()
        try:
            uow = ctx.get_bean(UnitOfWork)
            assert uow.factory is ctx.get_bean(async_sessionmaker)
            session = uow.factory()
            assert isinstance(session, AsyncSession)
            await session.close()
        finally:
            await ctx.stop()
