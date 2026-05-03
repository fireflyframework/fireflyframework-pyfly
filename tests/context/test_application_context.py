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
"""Tests for ApplicationContext — the central bean registry."""

import pytest

from pyfly.container.bean import bean
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.stereotypes import configuration, repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_bean,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.context.events import ApplicationReadyEvent, ContextRefreshedEvent
from pyfly.context.lifecycle import post_construct, pre_destroy
from pyfly.core.config import Config

# --- Test beans ---


@service
class GreetingService:
    def greet(self) -> str:
        return "Hello"


@repository
class UserRepository:
    def find(self, user_id: str) -> dict:
        return {"id": user_id}


@service
class UserService:
    def __init__(self, repo: UserRepository):
        self.repo = repo

    def get_user(self, user_id: str) -> dict:
        return self.repo.find(user_id)


@configuration
class AppConfig:
    @bean
    def greeting_message(self) -> str:
        return "Welcome!"


# --- Tests ---


class TestApplicationContextBasics:
    @pytest.mark.asyncio
    async def test_register_and_resolve(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(GreetingService)
        await ctx.start()
        svc = ctx.get_bean(GreetingService)
        assert svc.greet() == "Hello"

    @pytest.mark.asyncio
    async def test_dependency_injection(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserRepository)
        ctx.register_bean(UserService)
        await ctx.start()
        svc = ctx.get_bean(UserService)
        assert svc.get_user("u-1")["id"] == "u-1"

    @pytest.mark.asyncio
    async def test_get_bean_by_name(self):
        ctx = ApplicationContext(Config({}))

        @service(name="myService")
        class NamedService:
            pass

        ctx.register_bean(NamedService)
        await ctx.start()
        result = ctx.get_bean_by_name("myService")
        assert isinstance(result, NamedService)

    @pytest.mark.asyncio
    async def test_contains_bean(self):
        ctx = ApplicationContext(Config({}))

        @service(name="exists")
        class ExistingService:
            pass

        ctx.register_bean(ExistingService)
        await ctx.start()
        assert ctx.contains_bean("exists") is True
        assert ctx.contains_bean("missing") is False


class TestBeanFactoryMethods:
    @pytest.mark.asyncio
    async def test_configuration_bean_method(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(AppConfig)
        await ctx.start()
        msg = ctx.get_bean(str)
        assert msg == "Welcome!"


class TestLifecycleAnnotations:
    @pytest.mark.asyncio
    async def test_post_construct_called(self):
        log: list[str] = []

        @service
        class InitService:
            @post_construct
            async def setup(self):
                log.append("initialized")

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(InitService)
        await ctx.start()
        assert "initialized" in log

    @pytest.mark.asyncio
    async def test_pre_destroy_called(self):
        log: list[str] = []

        @service
        class CleanupService:
            @pre_destroy
            async def teardown(self):
                log.append("destroyed")

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(CleanupService)
        await ctx.start()
        await ctx.stop()
        assert "destroyed" in log


class TestApplicationEvents:
    @pytest.mark.asyncio
    async def test_context_refreshed_published(self):
        events: list = []

        ctx = ApplicationContext(Config({}))

        async def on_refreshed(event):
            events.append(event)

        ctx.event_bus.subscribe(ContextRefreshedEvent, on_refreshed)
        await ctx.start()
        assert any(isinstance(e, ContextRefreshedEvent) for e in events)

    @pytest.mark.asyncio
    async def test_application_ready_published(self):
        events: list = []

        ctx = ApplicationContext(Config({}))

        async def on_ready(event):
            events.append(event)

        ctx.event_bus.subscribe(ApplicationReadyEvent, on_ready)
        await ctx.start()
        assert any(isinstance(e, ApplicationReadyEvent) for e in events)


class TestEnvironmentAccess:
    @pytest.mark.asyncio
    async def test_environment_available(self):
        ctx = ApplicationContext(Config({"app": {"name": "test"}}))
        await ctx.start()
        assert ctx.environment.get_property("app.name") == "test"

    @pytest.mark.asyncio
    async def test_config_available(self):
        config = Config({"key": "value"})
        ctx = ApplicationContext(config)
        await ctx.start()
        assert ctx.config.get("key") == "value"


class TestProfileBasedBeanFiltering:
    @pytest.fixture()
    def ctx_with_profiles(self):
        """ApplicationContext with 'dev' and 'test' profiles active."""
        config = Config({"pyfly": {"profiles": {"active": "dev,test"}}})
        return ApplicationContext(config)

    async def test_bean_with_matching_profile_is_initialized(self, ctx_with_profiles):
        @service(profile="dev")
        class DevOnlyService:
            pass

        ctx_with_profiles.register_bean(DevOnlyService)
        await ctx_with_profiles.start()
        instance = ctx_with_profiles.get_bean(DevOnlyService)
        assert instance is not None

    async def test_bean_with_non_matching_profile_is_skipped(self, ctx_with_profiles):
        @service(profile="production")
        class ProdService:
            pass

        ctx_with_profiles.register_bean(ProdService)
        await ctx_with_profiles.start()
        with pytest.raises(NoSuchBeanError):
            ctx_with_profiles.get_bean(ProdService)

    async def test_bean_without_profile_is_always_initialized(self, ctx_with_profiles):
        @service
        class AlwaysService:
            pass

        ctx_with_profiles.register_bean(AlwaysService)
        await ctx_with_profiles.start()
        assert ctx_with_profiles.get_bean(AlwaysService) is not None

    async def test_negated_profile_bean(self, ctx_with_profiles):
        @service(profile="!production")
        class NonProdService:
            pass

        ctx_with_profiles.register_bean(NonProdService)
        await ctx_with_profiles.start()
        assert ctx_with_profiles.get_bean(NonProdService) is not None

    async def test_multi_profile_bean(self, ctx_with_profiles):
        @service(profile="dev,staging")
        class DevStagingService:
            pass

        ctx_with_profiles.register_bean(DevStagingService)
        await ctx_with_profiles.start()
        assert ctx_with_profiles.get_bean(DevStagingService) is not None

    async def test_bean_count_excludes_filtered(self, ctx_with_profiles):
        @service
        class IncludedService:
            pass

        @service(profile="production")
        class ExcludedService:
            pass

        ctx_with_profiles.register_bean(IncludedService)
        ctx_with_profiles.register_bean(ExcludedService)
        await ctx_with_profiles.start()
        assert ctx_with_profiles.bean_count >= 1


class TestOrderSorting:
    async def test_beans_initialized_in_order(self):
        from pyfly.container.ordering import order

        init_order: list[str] = []

        @order(2)
        @service
        class SecondService:
            def __init__(self):
                init_order.append("second")

        @order(1)
        @service
        class FirstService:
            def __init__(self):
                init_order.append("first")

        @order(3)
        @service
        class ThirdService:
            def __init__(self):
                init_order.append("third")

        config = Config({})
        ctx = ApplicationContext(config)
        ctx.register_bean(SecondService)
        ctx.register_bean(FirstService)
        ctx.register_bean(ThirdService)

        await ctx.start()
        assert init_order == ["first", "second", "third"]

    async def test_undecorated_beans_default_to_zero(self):
        from pyfly.container.ordering import order

        init_order: list[str] = []

        @order(-1)
        @service
        class EarlyService:
            def __init__(self):
                init_order.append("early")

        @service
        class DefaultService:
            def __init__(self):
                init_order.append("default")

        @order(1)
        @service
        class LateService:
            def __init__(self):
                init_order.append("late")

        config = Config({})
        ctx = ApplicationContext(config)
        ctx.register_bean(LateService)
        ctx.register_bean(DefaultService)
        ctx.register_bean(EarlyService)

        await ctx.start()
        assert init_order == ["early", "default", "late"]

    async def test_post_processors_sorted_by_order(self):
        from pyfly.container.ordering import order

        call_order: list[str] = []

        @order(2)
        class SecondPP:
            def before_init(self, bean, name):
                call_order.append("second-before")
                return bean

            def after_init(self, bean, name):
                return bean

        @order(1)
        class FirstPP:
            def before_init(self, bean, name):
                call_order.append("first-before")
                return bean

            def after_init(self, bean, name):
                return bean

        @service
        class Svc:
            pass

        config = Config({})
        ctx = ApplicationContext(config)
        ctx.register_bean(Svc)
        ctx.register_post_processor(SecondPP())
        ctx.register_post_processor(FirstPP())

        await ctx.start()
        # Post-processors run on each bean; verify ordering is consistent per bean
        assert call_order[0] == "first-before"
        assert call_order[1] == "second-before"

    async def test_get_beans_of_type_returns_sorted(self):
        from pyfly.container.ordering import order

        class Base:
            pass

        @order(3)
        @service
        class ThirdImpl(Base):
            pass

        @order(1)
        @service
        class FirstImpl(Base):
            pass

        config = Config({})
        ctx = ApplicationContext(config)
        ctx.register_bean(ThirdImpl)
        ctx.register_bean(FirstImpl)
        ctx.container.bind(Base, ThirdImpl)
        ctx.container.bind(Base, FirstImpl)

        await ctx.start()
        results = ctx.get_beans_of_type(Base)
        assert isinstance(results[0], FirstImpl)
        assert isinstance(results[1], ThirdImpl)


# ------------------------------------------------------------------
# Conditional evaluation integration tests
# ------------------------------------------------------------------


class TestConditionalOnPropertyIntegration:
    @pytest.mark.asyncio
    async def test_excluded_when_property_does_not_match(self):
        config = Config({"cache": {"enabled": "false"}})
        ctx = ApplicationContext(config)

        @conditional_on_property("cache.enabled", having_value="true")
        @service
        class CacheService:
            pass

        ctx.register_bean(CacheService)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(CacheService)

    @pytest.mark.asyncio
    async def test_included_when_property_matches(self):
        config = Config({"cache": {"enabled": "true"}})
        ctx = ApplicationContext(config)

        @conditional_on_property("cache.enabled", having_value="true")
        @service
        class CacheService:
            pass

        ctx.register_bean(CacheService)
        await ctx.start()
        assert ctx.get_bean(CacheService) is not None

    @pytest.mark.asyncio
    async def test_excluded_when_property_missing(self):
        config = Config({})
        ctx = ApplicationContext(config)

        @conditional_on_property("feature.enabled", having_value="true")
        @service
        class FeatureService:
            pass

        ctx.register_bean(FeatureService)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(FeatureService)


class TestConditionalOnClassIntegration:
    @pytest.mark.asyncio
    async def test_included_when_module_available(self):
        ctx = ApplicationContext(Config({}))

        @conditional_on_class("json")
        @service
        class JsonService:
            pass

        ctx.register_bean(JsonService)
        await ctx.start()
        assert ctx.get_bean(JsonService) is not None

    @pytest.mark.asyncio
    async def test_excluded_when_module_unavailable(self):
        ctx = ApplicationContext(Config({}))

        @conditional_on_class("nonexistent_xyz_module_12345")
        @service
        class MissingDepService:
            pass

        ctx.register_bean(MissingDepService)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(MissingDepService)


class TestConditionalOnMissingBeanIntegration:
    @pytest.mark.asyncio
    async def test_included_when_no_bean_exists(self):
        ctx = ApplicationContext(Config({}))

        class CachePort:
            pass

        @conditional_on_missing_bean(CachePort)
        @service
        class FallbackCache(CachePort):
            pass

        ctx.register_bean(FallbackCache)
        await ctx.start()
        assert ctx.get_bean(FallbackCache) is not None

    @pytest.mark.asyncio
    async def test_excluded_when_bean_exists(self):
        ctx = ApplicationContext(Config({}))

        class CachePort:
            pass

        @service
        class RedisCache(CachePort):
            pass

        @conditional_on_missing_bean(CachePort)
        @service
        class FallbackCache(CachePort):
            pass

        ctx.register_bean(RedisCache)
        ctx.register_bean(FallbackCache)
        await ctx.start()
        # RedisCache should survive, FallbackCache should be removed
        assert ctx.get_bean(RedisCache) is not None
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(FallbackCache)


class TestConditionalOnBeanIntegration:
    @pytest.mark.asyncio
    async def test_included_when_dependency_exists(self):
        ctx = ApplicationContext(Config({}))

        class DataSource:
            pass

        @service
        class PostgresDataSource(DataSource):
            pass

        @conditional_on_bean(DataSource)
        @service
        class TransactionManager:
            pass

        ctx.register_bean(PostgresDataSource)
        ctx.register_bean(TransactionManager)
        await ctx.start()
        assert ctx.get_bean(TransactionManager) is not None

    @pytest.mark.asyncio
    async def test_excluded_when_dependency_missing(self):
        ctx = ApplicationContext(Config({}))

        class DataSource:
            pass

        @conditional_on_bean(DataSource)
        @service
        class TransactionManager:
            pass

        ctx.register_bean(TransactionManager)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(TransactionManager)


class TestAutoConfigurationIntegration:
    @pytest.mark.asyncio
    async def test_auto_config_processed_after_user_config(self):
        processing_order: list[str] = []

        @configuration
        class UserConfig:
            @bean
            def user_message(self) -> str:
                processing_order.append("user")
                return "from user"

        @auto_configuration
        class FallbackConfig:
            @bean
            def fallback_value(self) -> int:
                processing_order.append("auto")
                return 42

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(UserConfig)
        ctx.register_bean(FallbackConfig)
        await ctx.start()

        assert processing_order == ["user", "auto"]
        assert ctx.get_bean(str) == "from user"
        assert ctx.get_bean(int) == 42

    @pytest.mark.asyncio
    async def test_auto_config_skipped_when_user_provides_bean(self):
        """@auto_configuration + @conditional_on_missing_bean: user bean wins."""
        ctx = ApplicationContext(Config({}))

        class CachePort:
            pass

        @service
        class UserCache(CachePort):
            pass

        @conditional_on_missing_bean(CachePort)
        @auto_configuration
        class AutoCacheConfig:
            @bean
            def cache(self) -> CachePort:
                return CachePort()

        ctx.register_bean(UserCache)
        ctx.register_bean(AutoCacheConfig)
        await ctx.start()

        # User bean should survive
        assert ctx.get_bean(UserCache) is not None
        # Auto-config should have been removed
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(AutoCacheConfig)


class TestStackedConditionsIntegration:
    @pytest.mark.asyncio
    async def test_both_conditions_pass(self):
        config = Config({"feature": {"enabled": "true"}})
        ctx = ApplicationContext(config)

        @conditional_on_class("json")
        @conditional_on_property("feature.enabled", having_value="true")
        @service
        class FeatureService:
            pass

        ctx.register_bean(FeatureService)
        await ctx.start()
        assert ctx.get_bean(FeatureService) is not None

    @pytest.mark.asyncio
    async def test_one_condition_fails(self):
        config = Config({"feature": {"enabled": "true"}})
        ctx = ApplicationContext(config)

        @conditional_on_class("nonexistent_xyz_module_12345")
        @conditional_on_property("feature.enabled", having_value="true")
        @service
        class FeatureService:
            pass

        ctx.register_bean(FeatureService)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(FeatureService)


class TestSingularConditionIntegration:
    @pytest.mark.asyncio
    async def test_stereotype_condition_true(self):
        ctx = ApplicationContext(Config({}))

        @service(condition=lambda: True)
        class AlwaysService:
            pass

        ctx.register_bean(AlwaysService)
        await ctx.start()
        assert ctx.get_bean(AlwaysService) is not None

    @pytest.mark.asyncio
    async def test_stereotype_condition_false(self):
        ctx = ApplicationContext(Config({}))

        @service(condition=lambda: False)
        class NeverService:
            pass

        ctx.register_bean(NeverService)
        await ctx.start()
        with pytest.raises(NoSuchBeanError):
            ctx.get_bean(NeverService)
