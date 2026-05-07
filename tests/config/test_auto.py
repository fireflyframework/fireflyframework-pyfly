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
"""Tests for AutoConfiguration provider detection and auto-configuration classes."""

from __future__ import annotations

import pytest

from pyfly.config.auto import AutoConfiguration, discover_auto_configurations
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.core.config import Config
from pyfly.kernel.exceptions import InfrastructureException


class TestAutoConfiguration:
    def test_detects_available_module(self):
        assert AutoConfiguration.is_available("json") is True

    def test_detects_unavailable_module(self):
        assert AutoConfiguration.is_available("nonexistent_xyz_module") is False

    def test_detect_cache_provider(self):
        from pyfly.cache.auto_configuration import CacheAutoConfiguration

        provider = CacheAutoConfiguration.detect_provider()
        assert provider in ("redis", "memory")

    def test_detect_messaging_provider(self):
        from pyfly.messaging.auto_configuration import MessagingAutoConfiguration

        provider = MessagingAutoConfiguration.detect_provider()
        assert provider in ("kafka", "rabbitmq", "memory")


class TestDiscoverAutoConfigurations:
    def test_returns_all_auto_config_classes(self):
        classes = discover_auto_configurations()
        assert len(classes) == 36

    def test_all_classes_have_auto_configuration_marker(self):
        for cls in discover_auto_configurations():
            assert getattr(cls, "__pyfly_auto_configuration__", False) is True, (
                f"{cls.__name__} missing __pyfly_auto_configuration__"
            )

    def test_all_classes_are_configuration_stereotype(self):
        for cls in discover_auto_configurations():
            assert getattr(cls, "__pyfly_stereotype__", "") == "configuration", (
                f"{cls.__name__} missing configuration stereotype"
            )

    def test_all_classes_have_conditions(self):
        # AopAutoConfiguration is unconditional (always active) — skip it
        unconditional = {"AopAutoConfiguration"}
        for cls in discover_auto_configurations():
            if cls.__name__ in unconditional:
                continue
            conditions = getattr(cls, "__pyfly_conditions__", [])
            assert len(conditions) > 0, f"{cls.__name__} has no conditions"

    def test_contains_expected_class_names(self):
        names = {cls.__name__ for cls in discover_auto_configurations()}
        assert names == {
            "ActuatorAutoConfiguration",
            "AdminAutoConfiguration",
            "AopAutoConfiguration",
            "CacheAutoConfiguration",
            "CallbacksAutoConfiguration",
            "ClientAutoConfiguration",
            "ConfigServerAutoConfiguration",
            "CqrsAutoConfiguration",
            "DocumentAutoConfiguration",
            "EcmAutoConfiguration",
            "EventLoopAutoConfiguration",
            "EventSourcingAutoConfiguration",
            "I18nAutoConfiguration",
            "IdpAutoConfiguration",
            "JwtAutoConfiguration",
            "MessagingAutoConfiguration",
            "MetricsActuatorAutoConfiguration",
            "MetricsAutoConfiguration",
            "NotificationsAutoConfiguration",
            "OAuth2AuthorizationServerAutoConfiguration",
            "OAuth2ClientAutoConfiguration",
            "OAuth2LoginAutoConfiguration",
            "OAuth2ResourceServerAutoConfiguration",
            "PasswordEncoderAutoConfiguration",
            "PluginsAutoConfiguration",
            "RelationalAutoConfiguration",
            "RuleEngineAutoConfiguration",
            "SchedulingAutoConfiguration",
            "ServerAutoConfiguration",
            "SessionFilterAutoConfiguration",
            "SessionStoreAutoConfiguration",
            "ShellAutoConfiguration",
            "TracingAutoConfiguration",
            "TransactionalEngineAutoConfiguration",
            "WebAutoConfiguration",
            "WebhooksAutoConfiguration",
        }


class TestAutoConfigurationClasses:
    """Test that @bean methods on auto-config classes produce correct types."""

    def test_web_auto_config_produces_web_adapter(self):
        from pyfly.web.auto_configuration import WebAutoConfiguration

        instance = WebAutoConfiguration()
        adapter = instance.web_adapter()
        from pyfly.web.adapters.starlette.adapter import StarletteWebAdapter

        assert isinstance(adapter, StarletteWebAdapter)

    def test_cache_auto_config_produces_memory_cache(self):
        from pyfly.cache.auto_configuration import CacheAutoConfiguration

        config = Config({"pyfly": {"cache": {"enabled": True, "provider": "memory"}}})
        instance = CacheAutoConfiguration()
        adapter = instance.cache_adapter(config)
        from pyfly.cache.adapters.memory import InMemoryCache

        assert isinstance(adapter, InMemoryCache)

    def test_messaging_auto_config_produces_memory_broker(self):
        from pyfly.messaging.auto_configuration import MessagingAutoConfiguration

        config = Config({"pyfly": {"messaging": {"provider": "memory"}}})
        instance = MessagingAutoConfiguration()
        broker = instance.message_broker(config)
        from pyfly.messaging.adapters.memory import InMemoryMessageBroker

        assert isinstance(broker, InMemoryMessageBroker)

    def test_messaging_auto_config_produces_kafka(self):
        from pyfly.messaging.auto_configuration import MessagingAutoConfiguration

        config = Config(
            {
                "pyfly": {
                    "messaging": {
                        "provider": "kafka",
                        "kafka": {"bootstrap-servers": "localhost:9092"},
                    }
                }
            }
        )
        instance = MessagingAutoConfiguration()
        broker = instance.message_broker(config)
        from pyfly.messaging.adapters.kafka import KafkaAdapter

        assert isinstance(broker, KafkaAdapter)

    def test_messaging_auto_config_produces_rabbitmq(self):
        from pyfly.messaging.auto_configuration import MessagingAutoConfiguration

        config = Config(
            {
                "pyfly": {
                    "messaging": {
                        "provider": "rabbitmq",
                        "rabbitmq": {"url": "amqp://guest:guest@localhost/"},
                    }
                }
            }
        )
        instance = MessagingAutoConfiguration()
        broker = instance.message_broker(config)
        from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

        assert isinstance(broker, RabbitMQAdapter)

    def test_client_auto_config_produces_httpx_adapter(self):
        from pyfly.client.auto_configuration import ClientAutoConfiguration

        config = Config({"pyfly": {"client": {"timeout": 10}}})
        instance = ClientAutoConfiguration()
        adapter = instance.http_client_adapter(config)
        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter

        assert isinstance(adapter, HttpxClientAdapter)

    def test_client_auto_config_produces_post_processor(self):
        from pyfly.client.auto_configuration import ClientAutoConfiguration
        from pyfly.client.post_processor import HttpClientBeanPostProcessor

        config = Config({"pyfly": {"client": {"timeout": 10}}})
        instance = ClientAutoConfiguration()
        pp = instance.http_client_post_processor(config)
        assert isinstance(pp, HttpClientBeanPostProcessor)

    def test_document_auto_config_produces_motor_client(self):
        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration

        config = Config(
            {
                "pyfly": {
                    "data": {
                        "document": {
                            "enabled": True,
                            "uri": "mongodb://localhost:27017",
                        }
                    }
                }
            }
        )
        instance = DocumentAutoConfiguration()
        client = instance.motor_client(config)
        from motor.motor_asyncio import AsyncIOMotorClient

        assert isinstance(client, AsyncIOMotorClient)

    def test_document_auto_config_produces_post_processor(self):
        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration
        from pyfly.data.document.mongodb.post_processor import (
            MongoRepositoryBeanPostProcessor,
        )

        instance = DocumentAutoConfiguration()
        pp = instance.mongo_post_processor()
        assert isinstance(pp, MongoRepositoryBeanPostProcessor)

    def test_relational_auto_config_produces_engine(self):
        from sqlalchemy.ext.asyncio import AsyncEngine

        from pyfly.data.relational.auto_configuration import (
            RelationalAutoConfiguration,
        )

        config = Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": "sqlite+aiosqlite:///:memory:",
                        }
                    }
                }
            }
        )
        instance = RelationalAutoConfiguration()
        engine = instance.async_engine(config)
        assert isinstance(engine, AsyncEngine)

    def test_relational_auto_config_produces_session(self):
        from sqlalchemy.ext.asyncio import AsyncSession

        from pyfly.data.relational.auto_configuration import (
            RelationalAutoConfiguration,
        )

        config = Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": "sqlite+aiosqlite:///:memory:",
                        }
                    }
                }
            }
        )
        instance = RelationalAutoConfiguration()
        engine = instance.async_engine(config)
        factory = instance.async_session_factory(engine)
        session = instance.async_session(factory)
        assert isinstance(session, AsyncSession)

    def test_relational_auto_config_produces_post_processor(self):
        from pyfly.data.relational.auto_configuration import (
            RelationalAutoConfiguration,
        )
        from pyfly.data.relational.sqlalchemy.post_processor import (
            RepositoryBeanPostProcessor,
        )

        instance = RelationalAutoConfiguration()
        pp = instance.repository_post_processor()
        assert isinstance(pp, RepositoryBeanPostProcessor)


class TestAutoConfigurationIntegration:
    """Test auto-configuration classes through ApplicationContext."""

    @pytest.mark.asyncio
    async def test_context_wires_memory_cache(self):
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.cache.ports.outbound import CacheAdapter
        from pyfly.context.application_context import ApplicationContext

        config = Config({"pyfly": {"cache": {"enabled": True, "provider": "memory"}}})
        ctx = ApplicationContext(config)
        await ctx.start()
        try:
            adapter = ctx.get_bean(CacheAdapter)
            assert isinstance(adapter, InMemoryCache)
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_context_wires_messaging(self):
        from pyfly.context.application_context import ApplicationContext
        from pyfly.messaging.adapters.memory import InMemoryMessageBroker
        from pyfly.messaging.ports.outbound import MessageBrokerPort

        config = Config({"pyfly": {"messaging": {"provider": "memory"}}})
        ctx = ApplicationContext(config)
        await ctx.start()
        try:
            broker = ctx.get_bean(MessageBrokerPort)
            assert isinstance(broker, InMemoryMessageBroker)
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_context_wires_httpx_client(self):
        from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter
        from pyfly.client.ports.outbound import HttpClientPort
        from pyfly.context.application_context import ApplicationContext

        config = Config({"pyfly": {"client": {"timeout": 5}}})
        ctx = ApplicationContext(config)
        await ctx.start()
        try:
            client = ctx.get_bean(HttpClientPort)
            assert isinstance(client, HttpxClientAdapter)
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_context_skips_disabled_cache(self):
        from pyfly.cache.ports.outbound import CacheAdapter
        from pyfly.context.application_context import ApplicationContext

        config = Config({"pyfly": {"cache": {"enabled": False}}})
        ctx = ApplicationContext(config)
        await ctx.start()
        try:
            with pytest.raises(NoSuchBeanError):
                ctx.get_bean(CacheAdapter)
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_user_bean_takes_precedence(self):
        """@conditional_on_missing_bean skips auto-config when user provides the bean."""
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.cache.ports.outbound import CacheAdapter
        from pyfly.container.types import Scope
        from pyfly.context.application_context import ApplicationContext

        config = Config({"pyfly": {"cache": {"enabled": True, "provider": "redis"}}})
        ctx = ApplicationContext(config)

        # Pre-register a user-provided cache adapter
        user_cache = InMemoryCache()
        ctx._container.register(InMemoryCache, scope=Scope.SINGLETON)
        ctx._container._registrations[InMemoryCache].instance = user_cache

        await ctx.start()
        try:
            # User bean is preserved and auto-config didn't overwrite it
            resolved = ctx.get_bean(InMemoryCache)
            assert resolved is user_cache
            # CacheAdapter should not have a SEPARATE registration (no auto-config ran)
            assert CacheAdapter not in ctx._container._registrations
        finally:
            await ctx.stop()


class TestFailFast:
    """Fail-fast: adapter lifecycle validates connectivity during context startup."""

    @pytest.mark.asyncio
    async def test_memory_providers_start_without_error(self):
        from pyfly.context.application_context import ApplicationContext

        config = Config(
            {
                "pyfly": {
                    "messaging": {"provider": "memory"},
                    "cache": {"enabled": True, "provider": "memory"},
                }
            }
        )
        ctx = ApplicationContext(config)
        await ctx.start()
        await ctx.stop()

    def test_bean_creation_exception_attributes(self):
        exc = BeanCreationException("messaging", "kafka", "Connection refused")
        assert exc.subsystem == "messaging"
        assert exc.provider == "kafka"
        assert exc.reason == "Connection refused"
        assert "messaging" in str(exc)
        assert "kafka" in str(exc)
        assert "Connection refused" in str(exc)

    def test_bean_creation_exception_inherits_infrastructure(self):
        exc = BeanCreationException("messaging", "kafka", "Connection refused")
        assert isinstance(exc, InfrastructureException)
        assert exc.code == "BEAN_CREATION_MESSAGING"

    def test_bean_creation_exception_has_error_code(self):
        exc = BeanCreationException("cache", "redis", "timeout")
        assert exc.code == "BEAN_CREATION_CACHE"
