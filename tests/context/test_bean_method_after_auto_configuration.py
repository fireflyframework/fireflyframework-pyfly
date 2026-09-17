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
"""A user ``@bean`` may depend on a bean an ``@auto_configuration`` provides.

User ``@configuration`` classes are processed BEFORE auto-configurations so that
``@conditional_on_missing_bean`` can see what the user declared. Until this suite existed that
ordering was also the order in which the ``@bean`` methods were CALLED, so a user factory
taking an auto-configured type (``async_sessionmaker``, ``EventPublisher``, a client pool)
failed with "No matching bean is registered" — the framework offered the bean and refused to
inject it. Applications worked around it by rebuilding the auto-configured object by hand.

The contract pinned here: a user ``@bean`` whose parameters are not yet registered is DEFERRED
until the auto-configurations have registered theirs, its declared return type still counts
for ``@conditional_on_missing_bean`` while it is deferred, and a dependency that nobody ever
registers is reported with the same ``NoSuchBeanError`` as before.
"""

from __future__ import annotations

import pytest

from pyfly.container.bean import bean
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.stereotypes import configuration, service
from pyfly.container.types import Scope
from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import auto_configuration, conditional_on_missing_bean
from pyfly.core.config import Config


class SessionFactory:
    """Stands in for ``async_sessionmaker`` — something only an auto-configuration builds."""

    def __init__(self, url: str) -> None:
        self.url = url


class Database:
    """A user-owned wrapper around the framework's factory."""

    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory


class Store:
    def __init__(self, database: Database) -> None:
        self.database = database


class Port:
    pass


class UserPort(Port):
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory


class AutoPort(Port):
    pass


class NeverRegistered:
    """Nothing registers this — the error path must stay as loud as it was."""


@auto_configuration
class SessionAutoConfiguration:
    @bean
    def session_factory(self) -> SessionFactory:
        return SessionFactory("postgresql://framework")


class TestUserBeanDependsOnAutoConfiguredBean:
    async def test_user_bean_receives_the_auto_configured_dependency(self) -> None:
        calls: list[str] = []

        @configuration
        class UserConfiguration:
            @bean
            def database(self, factory: SessionFactory) -> Database:
                calls.append("database")
                return Database(factory)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        await ctx.start()

        database = ctx.get_bean(Database)
        assert database.factory is ctx.get_bean(SessionFactory)
        assert database.factory.url == "postgresql://framework"
        # Deferred does not mean called twice: the factory ran exactly once.
        assert calls == ["database"]

    async def test_a_chain_of_user_beans_behind_an_auto_configured_one_resolves(self) -> None:
        @configuration
        class UserConfiguration:
            @bean
            def store(self, database: Database) -> Store:
                return Store(database)

            @bean
            def database(self, factory: SessionFactory) -> Database:
                return Database(factory)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        await ctx.start()

        store = ctx.get_bean(Store)
        assert store.database is ctx.get_bean(Database)
        assert store.database.factory is ctx.get_bean(SessionFactory)

    async def test_a_service_bean_may_inject_the_deferred_user_bean(self) -> None:
        @service
        class Reporter:
            def __init__(self, database: Database) -> None:
                self.database = database

        @configuration
        class UserConfiguration:
            @bean
            def database(self, factory: SessionFactory) -> Database:
                return Database(factory)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(Reporter)
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        await ctx.start()

        assert ctx.get_bean(Reporter).database is ctx.get_bean(Database)

    async def test_a_deferred_user_bean_still_backs_off_an_auto_configuration(self) -> None:
        """The user's declaration counts for ``@conditional_on_missing_bean`` before it is built.

        Otherwise deferring the user bean would let the auto-configuration register its own
        fallback in the window between step 2 and the deferred pass, and the application would
        end up with two beans for one port — the user's, and the one it meant to replace.
        """

        @configuration
        class UserConfiguration:
            @bean
            def port(self, factory: SessionFactory) -> Port:
                return UserPort(factory)

        @conditional_on_missing_bean(Port)
        @auto_configuration
        class FallbackPortConfiguration:
            @bean
            def port(self) -> Port:
                return AutoPort()

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        ctx.register_bean(FallbackPortConfiguration)
        await ctx.start()

        port = ctx.get_bean(Port)
        assert isinstance(port, UserPort)
        assert ctx.get_bean(UserPort) is port
        assert len(ctx.get_beans_of_type(Port)) == 1
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(FallbackPortConfiguration)

    async def test_a_dependency_nobody_registers_is_still_reported(self) -> None:
        @configuration
        class UserConfiguration:
            @bean
            def database(self, missing: NeverRegistered) -> Database:  # pragma: no cover - never called
                raise AssertionError("must not be called")

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        with pytest.raises(NoSuchBeanError) as excinfo:
            await ctx.start()
        assert "NeverRegistered" in str(excinfo.value)
        assert "UserConfiguration.database()" in str(excinfo.value)
        # The provisional registration must not survive the failure: a later resolve would
        # otherwise trip over the same missing dependency with a less precise message.
        assert not ctx.container.contains_type(Database)

    async def test_a_transient_user_bean_deferred_behind_an_auto_configuration_rebuilds_each_time(self) -> None:
        @configuration
        class UserConfiguration:
            @bean(scope=Scope.TRANSIENT)
            def database(self, factory: SessionFactory) -> Database:
                return Database(factory)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        await ctx.start()

        first = ctx.get_bean(Database)
        second = ctx.get_bean(Database)
        assert first is not second
        assert first.factory is second.factory is ctx.get_bean(SessionFactory)

    async def test_a_deferred_bean_with_a_name_is_reachable_by_name(self) -> None:
        @configuration
        class UserConfiguration:
            @bean(name="primaryDatabase")
            def database(self, factory: SessionFactory) -> Database:
                return Database(factory)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        await ctx.start()

        assert ctx.get_bean_by_name("primaryDatabase") is ctx.get_bean(Database)

    async def test_eager_user_beans_are_still_processed_before_auto_configurations(self) -> None:
        """The deferral is per bean, not per configuration: what CAN be built early still is."""
        order: list[str] = []

        @configuration
        class UserConfiguration:
            @bean
            def greeting(self) -> str:
                order.append("user:greeting")
                return "hello"

            @bean
            def database(self, factory: SessionFactory) -> Database:
                order.append("user:database")
                return Database(factory)

        @auto_configuration
        class CountingAutoConfiguration:
            @bean
            def answer(self) -> int:
                order.append("auto:answer")
                return 42

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfiguration)
        ctx.register_bean(SessionAutoConfiguration)
        ctx.register_bean(CountingAutoConfiguration)
        await ctx.start()

        assert order[0] == "user:greeting"
        assert order.index("auto:answer") < order.index("user:database")
