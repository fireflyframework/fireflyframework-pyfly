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
"""Tests for DefaultQueryBus pipeline with caching."""

from dataclasses import dataclass
from datetime import timedelta

import pytest

from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.exceptions import QueryProcessingException
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Query
from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.cqrs.validation.types import ValidationResult

# -- Test messages ----------------------------------------------------------


@dataclass
class GetOrderQuery(Query[dict]):
    order_id: str = ""


@dataclass
class FailingQuery(Query[None]):
    pass


@dataclass
class InvalidQuery(Query[None]):
    term: str = ""

    async def validate(self) -> ValidationResult:
        if not self.term:
            return ValidationResult.failure("term", "term is required")
        return ValidationResult.success()


# -- Test handlers ----------------------------------------------------------


class GetOrderHandler(QueryHandler[GetOrderQuery, dict]):
    async def do_handle(self, query: GetOrderQuery) -> dict:
        return {"id": query.order_id, "status": "shipped"}


class ContextAwareGetOrderHandler(QueryHandler[GetOrderQuery, dict]):
    async def do_handle_with_context(self, query: GetOrderQuery, context) -> dict:
        return {"id": query.order_id, "user": context.user_id}


class FailingQueryHandler(QueryHandler[FailingQuery, None]):
    async def do_handle(self, query: FailingQuery) -> None:
        raise RuntimeError("Query handler crashed")


class InvalidQueryHandler(QueryHandler[InvalidQuery, None]):
    async def do_handle(self, query: InvalidQuery) -> None:
        return None


@query_handler(cacheable=True, cache_ttl=300)
class CacheableGetOrderHandler(QueryHandler[GetOrderQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def do_handle(self, query: GetOrderQuery) -> dict:
        self.call_count += 1
        return {"id": query.order_id, "status": "fresh"}


# -- Fake cache adapter -----------------------------------------------------


class FakeCacheAdapter:
    """In-memory cache adapter mimicking the CacheAdapter port."""

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self._store.get(key)

    async def put(self, key: str, value: object, ttl: timedelta | None = None) -> None:
        self._store[key] = value

    async def evict(self, key: str) -> None:
        self._store.pop(key, None)

    async def clear(self) -> None:
        self._store.clear()


# -- Tests ------------------------------------------------------------------


class TestDefaultQueryBus:
    @pytest.fixture(autouse=True)
    def _clear_correlation(self) -> None:
        CorrelationContext.clear()

    @pytest.fixture
    def registry(self) -> HandlerRegistry:
        return HandlerRegistry()

    @pytest.fixture
    def bus(self, registry: HandlerRegistry) -> DefaultQueryBus:
        return DefaultQueryBus(registry=registry)

    async def test_query_dispatches_to_correct_handler(self, bus: DefaultQueryBus, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetOrderHandler())
        result = await bus.query(GetOrderQuery(order_id="ord-1"))
        assert result == {"id": "ord-1", "status": "shipped"}

    async def test_query_with_context_passes_context(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(ContextAwareGetOrderHandler())
        bus = DefaultQueryBus(registry=registry)
        ctx = ExecutionContextBuilder().with_user_id("admin").build()

        result = await bus.query_with_context(GetOrderQuery(order_id="ord-2"), ctx)
        assert result == {"id": "ord-2", "user": "admin"}

    async def test_pipeline_validation_failure_raises(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(InvalidQueryHandler())
        validation = CommandValidationService()
        bus = DefaultQueryBus(registry=registry, validation=validation)

        with pytest.raises(QueryProcessingException) as exc_info:
            await bus.query(InvalidQuery(term=""))
        assert exc_info.value.cause is not None
        assert isinstance(exc_info.value.cause, CqrsValidationException)
        assert "term is required" in str(exc_info.value.cause)

    async def test_pipeline_validation_success_proceeds(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(InvalidQueryHandler())
        validation = CommandValidationService()
        bus = DefaultQueryBus(registry=registry, validation=validation)

        result = await bus.query(InvalidQuery(term="valid"))
        assert result is None

    async def test_handler_error_wraps_in_processing_exception(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(FailingQueryHandler())
        bus = DefaultQueryBus(registry=registry)

        with pytest.raises(QueryProcessingException) as exc_info:
            await bus.query(FailingQuery())
        assert exc_info.value.query_type is FailingQuery
        assert exc_info.value.cause is not None
        assert "Query handler crashed" in str(exc_info.value.cause)

    async def test_cache_hit_returns_cached_result(self, registry: HandlerRegistry) -> None:
        handler = CacheableGetOrderHandler()
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        query1 = GetOrderQuery(order_id="ord-cached")
        result1 = await bus.query(query1)
        assert result1 == {"id": "ord-cached", "status": "fresh"}
        assert handler.call_count == 1

        query2 = GetOrderQuery(order_id="ord-cached")
        result2 = await bus.query(query2)
        assert result2 == {"id": "ord-cached", "status": "fresh"}
        assert handler.call_count == 1  # handler not called again

    async def test_cache_miss_executes_handler(self, registry: HandlerRegistry) -> None:
        handler = CacheableGetOrderHandler()
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        result = await bus.query(GetOrderQuery(order_id="ord-new"))
        assert result == {"id": "ord-new", "status": "fresh"}
        assert handler.call_count == 1

    async def test_cache_disabled_when_query_not_cacheable(self, registry: HandlerRegistry) -> None:
        handler = CacheableGetOrderHandler()
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        q1 = GetOrderQuery(order_id="ord-1")
        q1.set_cacheable(False)
        await bus.query(q1)

        q2 = GetOrderQuery(order_id="ord-1")
        q2.set_cacheable(False)
        await bus.query(q2)

        assert handler.call_count == 2  # handler called each time

    async def test_cache_disabled_when_handler_not_cacheable(self, registry: HandlerRegistry) -> None:
        handler = GetOrderHandler()  # not decorated with cacheable=True
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        await bus.query(GetOrderQuery(order_id="ord-1"))
        await bus.query(GetOrderQuery(order_id="ord-1"))
        # no error, runs fine without caching

    async def test_clear_cache_evicts_key(self, registry: HandlerRegistry) -> None:
        handler = CacheableGetOrderHandler()
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        query = GetOrderQuery(order_id="ord-evict")
        await bus.query(query)
        assert handler.call_count == 1

        cache_key = f":cqrs:{query.get_cache_key()}"
        await bus.clear_cache(cache_key)

        await bus.query(GetOrderQuery(order_id="ord-evict"))
        assert handler.call_count == 2

    async def test_clear_all_cache(self, registry: HandlerRegistry) -> None:
        handler = CacheableGetOrderHandler()
        registry.register_query_handler(handler)
        cache = FakeCacheAdapter()
        bus = DefaultQueryBus(registry=registry, cache_adapter=cache)

        await bus.query(GetOrderQuery(order_id="ord-all"))
        assert handler.call_count == 1

        await bus.clear_all_cache()

        await bus.query(GetOrderQuery(order_id="ord-all"))
        assert handler.call_count == 2

    async def test_clear_cache_no_adapter_is_noop(self) -> None:
        registry = HandlerRegistry()
        bus = DefaultQueryBus(registry=registry, cache_adapter=None)
        await bus.clear_cache("some-key")  # should not raise
        await bus.clear_all_cache()  # should not raise

    async def test_register_handler(self, bus: DefaultQueryBus) -> None:
        handler = GetOrderHandler()
        bus.register_handler(handler)
        assert bus.has_handler(GetOrderQuery) is True

    async def test_unregister_handler(self, bus: DefaultQueryBus) -> None:
        bus.register_handler(GetOrderHandler())
        assert bus.has_handler(GetOrderQuery) is True
        bus.unregister_handler(GetOrderQuery)
        assert bus.has_handler(GetOrderQuery) is False

    async def test_has_handler_false_initially(self, bus: DefaultQueryBus) -> None:
        assert bus.has_handler(GetOrderQuery) is False

    async def test_correlation_id_set_from_query(self, bus: DefaultQueryBus, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetOrderHandler())
        query = GetOrderQuery(order_id="o1")
        query.set_correlation_id("my-corr")
        await bus.query(query)
        assert CorrelationContext.get_correlation_id() == "my-corr"

    async def test_correlation_id_auto_generated(self, bus: DefaultQueryBus, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetOrderHandler())
        query = GetOrderQuery(order_id="o1")
        assert query.get_correlation_id() is None
        await bus.query(query)
        assert query.get_correlation_id() is not None
        assert len(query.get_correlation_id()) == 36
