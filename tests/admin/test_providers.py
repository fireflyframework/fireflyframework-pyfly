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
"""Tests for admin data providers."""

from unittest.mock import AsyncMock, MagicMock

from pyfly.admin.providers.beans_provider import BeansProvider
from pyfly.admin.providers.config_provider import ConfigProvider
from pyfly.admin.providers.env_provider import EnvProvider
from pyfly.admin.providers.health_provider import HealthProvider
from pyfly.admin.providers.loggers_provider import LoggersProvider
from pyfly.admin.providers.overview_provider import OverviewProvider
from pyfly.container.types import Scope


def _make_mock_context():
    """Create a mock ApplicationContext with sample beans."""
    ctx = MagicMock()
    ctx.config = MagicMock()
    ctx.config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "pyfly.app.name": "test-app",
            "pyfly.app.version": "1.0.0",
            "pyfly.app.description": "Test Application",
        }.get(key, default)
    )
    ctx.environment = MagicMock()
    ctx.environment.active_profiles = ["dev"]
    ctx.bean_count = 5
    ctx.wiring_counts = {"event_listeners": 2, "scheduled": 1}
    ctx.get_bean_counts_by_stereotype.return_value = {"service": 3, "repository": 2, "rest_controller": 1}

    # Mock container registrations
    class FakeService:
        __pyfly_stereotype__ = "service"

    class FakeRepo:
        __pyfly_stereotype__ = "repository"

    reg1 = MagicMock()
    reg1.name = "fakeService"
    reg1.scope = Scope.SINGLETON
    reg1.instance = FakeService()
    reg1.condition = None

    reg2 = MagicMock()
    reg2.name = "fakeRepo"
    reg2.scope = Scope.SINGLETON
    reg2.instance = FakeRepo()
    reg2.condition = None

    ctx.container._registrations = {FakeService: reg1, FakeRepo: reg2}
    ctx.container.get_bean_metrics = MagicMock(return_value=None)
    return ctx


class TestBeansProvider:
    async def test_get_beans(self):
        ctx = _make_mock_context()
        provider = BeansProvider(ctx)
        result = await provider.get_beans()
        assert "beans" in result
        assert len(result["beans"]) == 2
        bean = result["beans"][0]
        assert "name" in bean
        assert "type" in bean
        assert "scope" in bean
        assert "stereotype" in bean


class TestHealthProvider:
    async def test_get_health(self):
        aggregator = MagicMock()
        aggregator.check = AsyncMock(
            return_value=MagicMock(
                status="UP",
                to_dict=MagicMock(return_value={"status": "UP", "components": {"db": {"status": "UP", "details": {}}}}),
            )
        )
        provider = HealthProvider(aggregator)
        result = await provider.get_health()
        assert result["status"] == "UP"


class TestOverviewProvider:
    async def test_get_overview(self):
        ctx = _make_mock_context()
        aggregator = MagicMock()
        aggregator.check = AsyncMock(
            return_value=MagicMock(status="UP", to_dict=MagicMock(return_value={"status": "UP", "components": {}}))
        )
        provider = OverviewProvider(ctx, aggregator)
        result = await provider.get_overview()
        assert result["app"]["name"] == "test-app"
        assert result["health"]["status"] == "UP"
        assert result["beans"]["total"] == 5
        assert "stereotypes" in result["beans"]


class TestEnvProvider:
    async def test_get_env(self):
        ctx = _make_mock_context()
        data = {"pyfly": {"web": {"port": 8080}, "cache": {"enabled": True}}}
        ctx.config.to_dict.return_value = data
        ctx.config.loaded_sources = ["pyfly-defaults.yaml", "pyfly.yaml"]
        provider = EnvProvider(ctx)
        result = await provider.get_env()
        assert "active_profiles" in result
        assert "properties" in result
        assert "sources" in result


class TestLoggersProvider:
    async def test_get_loggers(self):
        provider = LoggersProvider()
        result = await provider.get_loggers()
        assert "loggers" in result
        assert "levels" in result
        assert "ROOT" in result["loggers"]

    async def test_set_logger_level(self):
        provider = LoggersProvider()
        result = await provider.set_level("pyfly.admin.test_logger", "DEBUG")
        assert result["configuredLevel"] == "DEBUG"


class TestTransactionsProvider:
    async def test_get_transactions_empty(self):
        from pyfly.admin.providers.transactions_provider import TransactionsProvider

        ctx = _make_mock_context()
        provider = TransactionsProvider(ctx)
        result = await provider.get_transactions()
        assert result["saga_count"] == 0
        assert result["tcc_count"] == 0
        assert result["total"] == 0
        assert result["in_flight"] == 0
        assert result["sagas"] == []
        assert result["tcc"] == []

    async def test_get_transactions_with_saga_registry(self):
        from unittest.mock import MagicMock

        from pyfly.admin.providers.transactions_provider import TransactionsProvider
        from pyfly.transactional.saga.registry.saga_definition import SagaDefinition
        from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
        from pyfly.transactional.saga.registry.step_definition import StepDefinition

        ctx = _make_mock_context()

        # Create a mock saga registry with one definition
        registry = SagaRegistry()
        saga_def = SagaDefinition(name="order-saga", bean=object(), layer_concurrency=3)
        saga_def.steps["reserve"] = StepDefinition(
            id="reserve",
            retry=2,
            timeout_ms=5000,
        )
        saga_def.steps["charge"] = StepDefinition(
            id="charge",
            depends_on=["reserve"],
        )
        registry._sagas["order-saga"] = saga_def

        reg_entry = MagicMock()
        reg_entry.name = "sagaRegistry"
        reg_entry.instance = registry
        ctx.container._registrations[SagaRegistry] = reg_entry

        provider = TransactionsProvider(ctx)
        result = await provider.get_transactions()
        assert result["saga_count"] == 1
        assert result["total"] == 1
        assert result["sagas"][0]["name"] == "order-saga"
        assert result["sagas"][0]["step_count"] == 2
        assert result["sagas"][0]["layer_concurrency"] == 3

    async def test_get_transactions_with_tcc_registry(self):
        from unittest.mock import MagicMock

        from pyfly.admin.providers.transactions_provider import TransactionsProvider
        from pyfly.transactional.tcc.registry.participant_definition import ParticipantDefinition
        from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition
        from pyfly.transactional.tcc.registry.tcc_registry import TccRegistry

        ctx = _make_mock_context()

        # Create a mock TCC registry with one definition
        registry = TccRegistry()
        tcc_def = TccDefinition(
            name="payment-tcc",
            bean=object(),
            timeout_ms=30000,
            retry_enabled=True,
            max_retries=3,
        )
        tcc_def.participants["debit"] = ParticipantDefinition(
            id="debit",
            order=0,
            try_method=lambda: None,
            confirm_method=lambda: None,
            cancel_method=lambda: None,
        )
        registry._tccs["payment-tcc"] = tcc_def

        reg_entry = MagicMock()
        reg_entry.name = "tccRegistry"
        reg_entry.instance = registry
        ctx.container._registrations[TccRegistry] = reg_entry

        provider = TransactionsProvider(ctx)
        result = await provider.get_transactions()
        assert result["tcc_count"] == 1
        assert result["tcc"][0]["name"] == "payment-tcc"
        assert result["tcc"][0]["participant_count"] == 1
        assert result["tcc"][0]["participants"][0]["has_try"] is True
        assert result["tcc"][0]["participants"][0]["has_confirm"] is True
        assert result["tcc"][0]["participants"][0]["has_cancel"] is True


class TestCacheProvider:
    async def test_cache_provider_no_adapter(self):
        from pyfly.admin.providers.cache_provider import CacheProvider

        ctx = _make_mock_context()
        provider = CacheProvider(ctx)
        result = await provider.get_caches()
        assert result["available"] is False
        assert result["stats"] == {}
        assert result["keys"] == []

    async def test_cache_provider_with_stats(self):
        from pyfly.admin.providers.cache_provider import CacheProvider
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.cache.ports.outbound import CacheAdapter

        ctx = _make_mock_context()
        cache = InMemoryCache()
        await cache.put("k1", "v1")
        await cache.put("k2", "v2")

        reg = MagicMock()
        reg.name = "memoryCache"
        reg.instance = cache
        ctx.container._registrations[CacheAdapter] = reg

        provider = CacheProvider(ctx)
        result = await provider.get_caches()
        assert result["available"] is True
        assert result["type"] == "InMemoryCache"
        assert result["stats"]["size"] == 2
        assert sorted(result["keys"]) == ["k1", "k2"]

    async def test_cache_provider_evict_key(self):
        from pyfly.admin.providers.cache_provider import CacheProvider
        from pyfly.cache.adapters.memory import InMemoryCache
        from pyfly.cache.ports.outbound import CacheAdapter

        ctx = _make_mock_context()
        cache = InMemoryCache()
        await cache.put("target", "value")

        reg = MagicMock()
        reg.name = "memoryCache"
        reg.instance = cache
        ctx.container._registrations[CacheAdapter] = reg

        provider = CacheProvider(ctx)
        result = await provider.evict_cache("target")
        assert result["evicted"] is True
        assert result["key"] == "target"
        assert await cache.get("target") is None


class TestConfigProvider:
    async def test_get_config_grouped(self):
        ctx = _make_mock_context()
        data = {
            "pyfly": {
                "web": {"port": 8080, "adapter": "auto"},
                "cache": {"enabled": False, "provider": "memory"},
            }
        }
        ctx.config.to_dict.return_value = data
        provider = ConfigProvider(ctx)
        result = await provider.get_config()
        assert "groups" in result
