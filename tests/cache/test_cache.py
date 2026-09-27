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
"""Tests for cache abstraction, in-memory cache, @cache decorator, and CacheManager."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.cache.adapters import InMemoryCache
from pyfly.cache.decorators import cache
from pyfly.cache.manager import CacheManager
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import CacheValueError
from pyfly.data.relational.sqlalchemy.entity import Base


class CachedProduct(Base):
    """A mapped entity: the in-memory cache must never hold a live instance of it (C022)."""

    __tablename__ = "wp14_cached_product"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@dataclass
class ProductView:
    id: int
    name: str
    tags: list[str] = field(default_factory=list)


class ProductDto(BaseModel):
    id: int
    name: str


class WrapsAnEntity(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    product: Any


class TestInMemoryCache:
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.get("key1") == "value1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        c = InMemoryCache()
        assert await c.get("missing") is None

    @pytest.mark.asyncio
    async def test_evict(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.evict("key1") is True
        assert await c.get("key1") is None

    @pytest.mark.asyncio
    async def test_evict_missing_returns_false(self):
        c = InMemoryCache()
        assert await c.evict("missing") is False

    @pytest.mark.asyncio
    async def test_clear(self):
        c = InMemoryCache()
        await c.put("a", 1)
        await c.put("b", 2)
        await c.clear()
        assert await c.get("a") is None
        assert await c.get("b") is None

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        import asyncio

        c = InMemoryCache()
        await c.put("key", "value", ttl=timedelta(milliseconds=50))
        assert await c.get("key") == "value"
        await asyncio.sleep(0.1)
        assert await c.get("key") is None

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_stored_key(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.exists("key1") is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_missing_key(self):
        c = InMemoryCache()
        assert await c.exists("no-such-key") is False

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_expired_key(self):
        c = InMemoryCache()
        await c.put("key1", "value1", ttl=timedelta(seconds=0))
        assert await c.exists("key1") is False

    @pytest.mark.asyncio
    async def test_protocol_compliance(self):
        """InMemoryCache satisfies the CacheAdapter protocol."""
        c: CacheAdapter = InMemoryCache()
        await c.put("x", 42)
        assert await c.get("x") == 42


class TestInMemoryCacheValueSemantics:
    """InMemoryCache stores copies (C022): callers never share, or corrupt, one cached object."""

    async def test_a_value_changed_after_put_does_not_change_the_entry(self) -> None:
        c = InMemoryCache()
        value = {"id": 1, "tags": ["a"]}
        await c.put("k", value)
        value["tags"].append("changed-after-put")
        assert await c.get("k") == {"id": 1, "tags": ["a"]}

    async def test_every_get_returns_its_own_copy(self) -> None:
        c = InMemoryCache()
        await c.put("k", ProductView(1, "widget", ["new"]))
        first = await c.get("k")
        first.name = "B-preview-not-saved"
        first.tags.append("B")
        second = await c.get("k")
        assert first is not second
        assert second == ProductView(1, "widget", ["new"])

    async def test_copies_keep_their_type(self) -> None:
        c = InMemoryCache()
        await c.put("dto", ProductDto(id=7, name="gadget"))
        restored = await c.get("dto")
        assert isinstance(restored, ProductDto)
        assert restored == ProductDto(id=7, name="gadget")

    async def test_a_value_of_a_local_class_is_still_copied(self) -> None:
        class Local:
            def __init__(self, n: int) -> None:
                self.n = n

        c = InMemoryCache()
        original = Local(3)
        await c.put("local", original)
        restored = await c.get("local")
        assert restored is not original
        assert restored.n == 3

    async def test_put_if_absent_stores_a_copy(self) -> None:
        c = InMemoryCache()
        value = {"n": 1}
        assert await c.put_if_absent("k", value) is True
        value["n"] = 2
        assert await c.get("k") == {"n": 1}

    async def test_a_mapped_entity_is_refused(self) -> None:
        c = InMemoryCache()
        with pytest.raises(CacheValueError, match="CachedProduct"):
            await c.put("p", CachedProduct(id=1, name="widget"))
        assert await c.exists("p") is False

    async def test_a_mapped_entity_inside_a_container_is_refused(self) -> None:
        c = InMemoryCache()
        with pytest.raises(CacheValueError, match="CachedProduct"):
            await c.put("list", [CachedProduct(id=1, name="a"), CachedProduct(id=2, name="b")])
        with pytest.raises(CacheValueError, match="CachedProduct"):
            await c.put("dto", WrapsAnEntity(product=CachedProduct(id=1, name="a")))
        with pytest.raises(CacheValueError, match="CachedProduct"):
            await c.put_if_absent("dict", {"page": [CachedProduct(id=1, name="a")]})
        assert c.get_keys() == []

    async def test_a_beanie_document_is_refused(self) -> None:
        from beanie import Document

        class CachedNote(Document):
            text: str

        c = InMemoryCache()
        with pytest.raises(CacheValueError, match="CachedNote"):
            await c.put("note", CachedNote.model_construct(text="hi"))
        with pytest.raises(CacheValueError, match="CachedNote"):
            await c.put("notes", {"items": [CachedNote.model_construct(text="hi")]})

    def test_cache_value_error_is_a_type_error(self) -> None:
        # Code that caught the old serializer's TypeError keeps working.
        assert issubclass(CacheValueError, TypeError)


class TestCacheDecorator:
    @pytest.mark.asyncio
    async def test_caches_result(self):
        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": "Alice"}

        result1 = await get_user("123")
        result2 = await get_user("123")
        assert result1 == result2
        assert call_count == 1  # Second call served from cache

    @pytest.mark.asyncio
    async def test_different_keys_not_shared(self):
        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        await get_user("1")
        await get_user("2")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cache_with_ttl(self):
        import asyncio

        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="data:{k}", ttl=timedelta(milliseconds=50))
        async def get_data(k: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result-{k}"

        await get_data("x")
        assert call_count == 1
        await asyncio.sleep(0.1)
        await get_data("x")
        assert call_count == 2  # TTL expired, re-fetched


class TestCacheManager:
    @pytest.mark.asyncio
    async def test_uses_primary(self):
        primary = InMemoryCache()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback)

        await manager.put("key", "value")
        assert await manager.get("key") == "value"
        # Also written to fallback
        assert await fallback.get("key") == "value"

    @pytest.mark.asyncio
    async def test_failover_to_fallback(self):
        class FailingCache:
            async def get(self, key: str):
                raise ConnectionError("Redis down")

            async def put(self, key: str, value, ttl=None):
                raise ConnectionError("Redis down")

            async def evict(self, key: str) -> bool:
                raise ConnectionError("Redis down")

            async def clear(self) -> None:
                raise ConnectionError("Redis down")

        fallback = InMemoryCache()
        await fallback.put("key", "cached-value")
        manager = CacheManager(primary=FailingCache(), fallback=fallback)

        # Should fall back to InMemoryCache
        result = await manager.get("key")
        assert result == "cached-value"

    @pytest.mark.asyncio
    async def test_evict_from_both(self):
        primary = InMemoryCache()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback)

        await manager.put("key", "value")
        await manager.evict("key")
        assert await primary.get("key") is None
        assert await fallback.get("key") is None
