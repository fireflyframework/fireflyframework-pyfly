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
"""Tests for the enhanced QueryHandler with lifecycle hooks and caching."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cqrs.context.execution_context import DefaultExecutionContext
from pyfly.cqrs.query.handler import ContextAwareQueryHandler, QueryHandler
from pyfly.cqrs.types import Query

# ── test query types ──────────────────────────────────────────


@dataclass(frozen=True)
class GetItemQuery(Query[dict]):
    item_id: str = "item-1"


@dataclass(frozen=True)
class SearchItemsQuery(Query[list[dict]]):
    keyword: str = "widget"


# ── handler that tracks lifecycle hook call order ─────────────


class TrackingQueryHandler(QueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def pre_process(self, query: GetItemQuery) -> None:
        self.calls.append("pre_process")

    async def do_handle(self, query: GetItemQuery) -> dict:
        self.calls.append("do_handle")
        return {"id": query.item_id, "name": "Widget"}

    async def post_process(self, query: GetItemQuery, result: dict) -> None:
        self.calls.append("post_process")

    async def on_success(self, query: GetItemQuery, result: dict) -> None:
        self.calls.append("on_success")

    async def on_error(self, query: GetItemQuery, error: Exception) -> None:
        self.calls.append("on_error")


# ── handler that raises in do_handle ──────────────────────────


class FailingQueryHandler(QueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def pre_process(self, query: GetItemQuery) -> None:
        self.calls.append("pre_process")

    async def do_handle(self, query: GetItemQuery) -> dict:
        self.calls.append("do_handle")
        raise LookupError("not found")

    async def post_process(self, query: GetItemQuery, result: dict) -> None:
        self.calls.append("post_process")

    async def on_success(self, query: GetItemQuery, result: dict) -> None:
        self.calls.append("on_success")

    async def on_error(self, query: GetItemQuery, error: Exception) -> None:
        self.calls.append("on_error")


# ── handler that transforms errors via map_error ──────────────


class MappingErrorQueryHandler(QueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def do_handle(self, query: GetItemQuery) -> dict:
        raise LookupError("original")

    async def on_error(self, query: GetItemQuery, error: Exception) -> None:
        self.calls.append("on_error")

    def map_error(self, query: GetItemQuery, error: Exception) -> Exception:
        self.calls.append("map_error")
        return RuntimeError(f"mapped: {error}")


# ── context-aware handler ─────────────────────────────────────


class ContextAwareGetHandler(ContextAwareQueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.captured_user_id: str | None = None

    async def do_handle_with_context(
        self,
        query: GetItemQuery,
        context: DefaultExecutionContext,
    ) -> dict:
        self.calls.append("do_handle_with_context")
        self.captured_user_id = context.user_id
        return {"id": query.item_id, "viewer": context.user_id}


# ── handler with custom do_handle_with_context ────────────────


class DelegatingQueryHandler(QueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def do_handle(self, query: GetItemQuery) -> dict:
        self.calls.append("do_handle")
        return {"id": query.item_id}

    async def do_handle_with_context(
        self,
        query: GetItemQuery,
        context: DefaultExecutionContext,
    ) -> dict:
        self.calls.append("do_handle_with_context")
        return {"id": query.item_id, "ctx": True}


# ── cacheable handler with decorator metadata ─────────────────


class CacheableQueryHandler(QueryHandler[GetItemQuery, dict]):
    __pyfly_cacheable__: bool = True
    __pyfly_cache_ttl__: int = 300

    def __init__(self) -> None:
        super().__init__()

    async def do_handle(self, query: GetItemQuery) -> dict:
        return {"id": query.item_id}


class NonCacheableQueryHandler(QueryHandler[GetItemQuery, dict]):
    def __init__(self) -> None:
        super().__init__()

    async def do_handle(self, query: GetItemQuery) -> dict:
        return {"id": query.item_id}


# ── lifecycle hook order tests ────────────────────────────────


class TestQueryHandlerLifecycle:
    @pytest.mark.asyncio
    async def test_success_hook_order(self) -> None:
        handler = TrackingQueryHandler()
        result = await handler.handle(GetItemQuery(item_id="item-42"))

        assert result == {"id": "item-42", "name": "Widget"}
        assert handler.calls == ["pre_process", "do_handle", "post_process", "on_success"]

    @pytest.mark.asyncio
    async def test_on_error_called_on_exception(self) -> None:
        handler = FailingQueryHandler()

        with pytest.raises(LookupError, match="not found"):
            await handler.handle(GetItemQuery())

        assert "pre_process" in handler.calls
        assert "do_handle" in handler.calls
        assert "on_error" in handler.calls
        assert "post_process" not in handler.calls
        assert "on_success" not in handler.calls

    @pytest.mark.asyncio
    async def test_error_hook_order(self) -> None:
        handler = FailingQueryHandler()

        with pytest.raises(LookupError):
            await handler.handle(GetItemQuery())

        assert handler.calls == ["pre_process", "do_handle", "on_error"]

    @pytest.mark.asyncio
    async def test_map_error_transforms_exception(self) -> None:
        handler = MappingErrorQueryHandler()

        with pytest.raises(RuntimeError, match="mapped: original"):
            await handler.handle(GetItemQuery())

        assert "on_error" in handler.calls
        assert "map_error" in handler.calls

    @pytest.mark.asyncio
    async def test_map_error_preserves_cause_chain(self) -> None:
        handler = MappingErrorQueryHandler()

        with pytest.raises(RuntimeError) as exc_info:
            await handler.handle(GetItemQuery())

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, LookupError)


# ── handle_with_context tests ─────────────────────────────────


class TestQueryHandlerWithContext:
    @pytest.mark.asyncio
    async def test_handle_with_context_delegates_to_do_handle_with_context(self) -> None:
        handler = DelegatingQueryHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        result = await handler.handle_with_context(GetItemQuery(), ctx)

        assert result == {"id": "item-1", "ctx": True}
        assert "do_handle_with_context" in handler.calls
        assert "do_handle" not in handler.calls

    @pytest.mark.asyncio
    async def test_default_do_handle_with_context_falls_back_to_do_handle(self) -> None:
        handler = TrackingQueryHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        result = await handler.handle_with_context(GetItemQuery(item_id="item-7"), ctx)

        assert result == {"id": "item-7", "name": "Widget"}
        assert "do_handle" in handler.calls

    @pytest.mark.asyncio
    async def test_handle_with_context_lifecycle_on_success(self) -> None:
        handler = TrackingQueryHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        await handler.handle_with_context(GetItemQuery(), ctx)

        assert handler.calls == ["pre_process", "do_handle", "post_process", "on_success"]

    @pytest.mark.asyncio
    async def test_handle_with_context_lifecycle_on_error(self) -> None:
        handler = FailingQueryHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        with pytest.raises(LookupError):
            await handler.handle_with_context(GetItemQuery(), ctx)

        assert handler.calls == ["pre_process", "do_handle", "on_error"]


# ── ContextAwareQueryHandler tests ────────────────────────────


class TestContextAwareQueryHandler:
    @pytest.mark.asyncio
    async def test_handle_without_context_raises_runtime_error(self) -> None:
        handler = ContextAwareGetHandler()

        with pytest.raises(RuntimeError, match="requires an ExecutionContext"):
            await handler.handle(GetItemQuery())

    @pytest.mark.asyncio
    async def test_handle_with_context_succeeds(self) -> None:
        handler = ContextAwareGetHandler()
        ctx = DefaultExecutionContext(user_id="viewer-1")

        result = await handler.handle_with_context(GetItemQuery(item_id="item-99"), ctx)

        assert result == {"id": "item-99", "viewer": "viewer-1"}
        assert handler.captured_user_id == "viewer-1"
        assert "do_handle_with_context" in handler.calls

    @pytest.mark.asyncio
    async def test_error_message_includes_class_name(self) -> None:
        handler = ContextAwareGetHandler()

        with pytest.raises(RuntimeError, match="ContextAwareGetHandler"):
            await handler.handle(GetItemQuery())


# ── caching metadata tests ────────────────────────────────────


class TestQueryHandlerCaching:
    def test_supports_caching_true_when_attribute_set(self) -> None:
        handler = CacheableQueryHandler()
        assert handler.supports_caching() is True

    def test_supports_caching_false_when_attribute_missing(self) -> None:
        handler = NonCacheableQueryHandler()
        assert handler.supports_caching() is False

    def test_get_cache_ttl_seconds_when_set(self) -> None:
        handler = CacheableQueryHandler()
        assert handler.get_cache_ttl_seconds() == 300

    def test_get_cache_ttl_seconds_none_when_missing(self) -> None:
        handler = NonCacheableQueryHandler()
        assert handler.get_cache_ttl_seconds() is None

    def test_supports_caching_reads_class_attribute(self) -> None:
        handler = TrackingQueryHandler()
        assert handler.supports_caching() is False


# ── get_query_type tests ──────────────────────────────────────


class TestGetQueryType:
    def test_resolves_generic_type_arg(self) -> None:
        handler = TrackingQueryHandler()
        assert handler.get_query_type() is GetItemQuery

    def test_resolves_for_context_aware_handler(self) -> None:
        handler = ContextAwareGetHandler()
        assert handler.get_query_type() is GetItemQuery

    def test_base_handler_returns_none(self) -> None:
        handler = QueryHandler()
        assert handler.get_query_type() is None
