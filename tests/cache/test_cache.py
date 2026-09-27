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

import fnmatch
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import pytest
from pydantic import BaseModel, ConfigDict, Field, computed_field
from pydantic.alias_generators import to_camel
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.cache.adapters import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cache.decorators import cache, cache_put, cacheable
from pyfly.cache.manager import CacheManager
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.serialization import CacheValueError, restore
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


class CamelDto(BaseModel):
    """camelCase aliases, the usual API style: the JSON cache stores field names."""

    model_config = ConfigDict(alias_generator=to_camel)

    user_id: int
    display_name: str


class AliasedOrderDto(BaseModel):
    order_no: str = Field(alias="orderNo")


class PricedLineDto(BaseModel):
    """A computed field and ``extra="forbid"``: the stored value must not carry the computed field."""

    model_config = ConfigDict(extra="forbid")

    quantity: int
    unit_price: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> Decimal:
        return self.quantity * self.unit_price


class TeamDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    team_name: str
    members: list[CamelDto]
    lines: list[PricedLineDto] = []


class Priced(Protocol):
    """A structural return type: ``isinstance`` refuses a protocol that is not runtime-checkable."""

    price: int


@runtime_checkable
class Named(Protocol):
    name: str


@dataclass
class PricedItem:
    price: int
    name: str = "item"


class RedisBytesStub:
    """The bytes a ``redis.asyncio`` client keeps, so a :class:`RedisCacheAdapter` runs its JSON code path."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.store.pop(key, None) is not None)

    async def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if key in self.store)

    async def scan_iter(self, match: str = "*", count: int | None = None):  # noqa: ANN201
        for key in list(self.store):
            if fnmatch.fnmatchcase(key, match):
                yield key


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

    async def test_expired_entries_do_not_accumulate(self) -> None:
        import asyncio

        c = InMemoryCache()
        for n in range(2000):
            await c.put(f"short:{n}", n, ttl=timedelta(milliseconds=10))
        await asyncio.sleep(0.05)
        for n in range(2000):
            await c.put(f"long:{n}", n, ttl=timedelta(minutes=5))
        # The expired entries are gone from memory, not only hidden: an unbounded cache stays bounded by
        # its TTLs even when no one reads an expired key again.
        assert len(c._store) == 2000
        assert all(key.startswith("long:") for key in c._store)

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


class TestDecoratorTypes:
    """The declared return type is checked when a method is decorated (C025) and rebuilt on a hit."""

    def test_an_entity_return_type_is_refused_when_decorated(self) -> None:
        with pytest.raises(TypeError, match="CachedProduct"):

            @cacheable(InMemoryCache(), key="p:{item_id}")
            async def get_item(item_id: int) -> CachedProduct | None: ...

        with pytest.raises(TypeError, match="CachedProduct"):

            @cache_put(InMemoryCache(), key="all")
            async def list_items() -> list[CachedProduct]: ...

    async def test_a_forward_referenced_entity_type_is_refused_before_the_first_call_runs(self) -> None:
        ran: list[str] = []

        @cacheable(InMemoryCache(), key="later:{item_id}")
        async def get_later(item_id: int) -> "LaterEntity":  # noqa: F821 — resolved after decoration
            ran.append("body")
            raise AssertionError("never reached")

        globals()["LaterEntity"] = CachedProduct
        try:
            with pytest.raises(TypeError, match="CachedProduct"):
                await get_later(1)
            assert ran == []
        finally:
            del globals()["LaterEntity"]

    async def test_a_dto_hit_comes_back_as_the_declared_type(self) -> None:
        backend = InMemoryCache()
        calls: list[int] = []

        @cacheable(backend, key="dto:{item_id}")
        async def get_dto(item_id: int) -> ProductDto:
            calls.append(item_id)
            return ProductDto(id=item_id, name="gadget")

        first = await get_dto(7)
        first.name = "changed by the first caller"
        second = await get_dto(7)
        assert calls == [7]
        assert isinstance(second, ProductDto)
        assert second == ProductDto(id=7, name="gadget")

    @pytest.mark.parametrize("protocol", [Priced, Named], ids=["protocol", "runtime_checkable_protocol"])
    @pytest.mark.parametrize("json", [False, True], ids=["in_memory", "json"])
    async def test_a_protocol_return_type_keeps_its_hits(
        self, protocol: Any, json: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend: CacheAdapter = RedisCacheAdapter(RedisBytesStub()) if json else InMemoryCache()
        calls: list[int] = []

        @cacheable(backend, key="item:{n}")
        async def get_item(n: int) -> protocol:  # type: ignore[valid-type]
            calls.append(n)
            return PricedItem(price=n)

        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        results = [await get_item(1) for _ in range(3)]
        assert calls == [1]  # a structural type cannot be rebuilt: the hit is served as stored
        assert [r.getMessage() for r in caplog.records if "cache_hit_discarded" in r.getMessage()] == []
        if not json:
            assert results == [PricedItem(price=1)] * 3

    async def test_an_iterable_return_type_hit_is_the_list_that_was_stored(self) -> None:
        backend = RedisCacheAdapter(RedisBytesStub())
        calls: list[int] = []

        @cacheable(backend, key="catalog")
        async def catalog() -> Iterable[ProductDto]:
            calls.append(1)
            return [ProductDto(id=1, name="gadget"), ProductDto(id=2, name="gizmo")]

        await catalog()
        hit = await catalog()
        assert calls == [1]
        assert isinstance(hit, list)
        assert list(hit) == list(hit) == [ProductDto(id=1, name="gadget"), ProductDto(id=2, name="gizmo")]


class TestRestore:
    """:func:`restore` rebuilds what it can and never refuses a value because of an unusable annotation."""

    def test_a_protocol_annotation_returns_the_value_as_stored(self) -> None:
        item = PricedItem(price=3)
        assert restore(item, Priced) is item
        assert restore({"price": 3}, Priced) == {"price": 3}
        assert restore(item, Named) is item
        assert restore({"name": "x"}, Named) == {"name": "x"}

    def test_an_iterable_annotation_is_rebuilt_as_a_list(self) -> None:
        restored = restore([{"id": 1, "name": "gadget"}], Iterable[ProductDto])
        assert restored == [ProductDto(id=1, name="gadget")]
        assert isinstance(restored, list)


_ALIASED_VALUES = [
    pytest.param(CamelDto, CamelDto(userId=1, displayName="Ada"), id="alias_generator"),
    pytest.param(AliasedOrderDto, AliasedOrderDto(orderNo="A-1"), id="field_alias"),
    pytest.param(PricedLineDto, PricedLineDto(quantity=2, unit_price=Decimal("1.25")), id="computed_field"),
    pytest.param(list[CamelDto], [CamelDto(userId=1, displayName="Ada")], id="list_of_aliased"),
    pytest.param(
        TeamDto,
        TeamDto(
            teamName="core",
            members=[CamelDto(userId=2, displayName="Grace")],
            lines=[PricedLineDto(quantity=1, unit_price=Decimal("3"))],
        ),
        id="nested",
    ),
]


class TestRedisNamespaces:
    """A dedicated cache never lives inside the namespace its source's ``clear()`` deletes (C034)."""

    @pytest.mark.parametrize("namespace", ["myapp", "myapp:", "myapp."])
    async def test_clearing_a_custom_namespace_keeps_its_dedicated_caches(self, namespace: str) -> None:
        client = RedisBytesStub()
        root = RedisCacheAdapter(client, namespace=namespace)
        idempotency = root.with_namespace("idempotency")
        other_app = RedisCacheAdapter(client, namespace="myapp2")  # another application's cache
        await root.put("product:1", {"id": 1})
        await idempotency.put("idem:k1", {"status": 201})
        await other_app.put("product:1", {"id": 2})

        await root.clear()
        assert await root.exists("product:1") is False
        assert await idempotency.get("idem:k1") == {"status": 201}
        assert await other_app.get("product:1") == {"id": 2}
        assert root.namespace.endswith(":")

    async def test_a_dedicated_cache_of_a_cache_that_owns_the_database_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = RedisBytesStub()
        root = RedisCacheAdapter(client, namespace="")  # owns the whole database: clear() deletes every key
        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        idempotency = root.with_namespace("idempotency")
        root.with_namespace("orchestration")
        await idempotency.put("idem:k1", {"status": 201})
        await root.clear()
        assert await idempotency.get("idem:k1") is None  # the database was the root cache's to clear
        warnings = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cache_not_dedicated")]
        assert len(warnings) == 1 and "namespace" in warnings[0]


class TestJsonHits:
    """A JSON cache (Redis, PostgreSQL) stores what the encoder writes; every hit must come back (C025)."""

    @pytest.mark.parametrize(("annotation", "value"), _ALIASED_VALUES)
    async def test_an_aliased_or_computed_dto_hit_comes_back_as_the_declared_type(
        self, annotation: Any, value: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = RedisCacheAdapter(RedisBytesStub())
        calls: list[int] = []

        @cacheable(backend, key="dto")
        async def load() -> annotation:  # type: ignore[valid-type]
            calls.append(1)
            return value

        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        results = [await load() for _ in range(3)]
        assert calls == [1]
        assert results == [value, value, value]
        assert type(results[-1]) is type(value)
        assert [r.getMessage() for r in caplog.records if "cache_hit_discarded" in r.getMessage()] == []


class _EvictionFails(InMemoryCache):
    async def evict(self, key: str) -> bool:
        raise ConnectionError("cache server unreachable")

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        raise ConnectionError("cache server unreachable")


class TestDecoratorRegionsAndFailures:
    async def test_all_entries_clears_only_the_named_region(self) -> None:
        from pyfly.cache.decorators import cache_evict

        backend = InMemoryCache()
        await backend.put("idem:POST:/pay:k1", "durable")

        @cacheable(backend, key="u:{user_id}", cache_name="users")
        async def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        @cacheable(backend, key="o:{order_id}", cache_name="orders")
        async def get_order(order_id: int) -> dict[str, int]:
            return {"id": order_id}

        @cache_evict(backend, all_entries=True, cache_name="users")
        async def reset_users() -> None: ...

        await get_user(1)
        await get_order(2)
        assert sorted(backend.get_keys()) == ["idem:POST:/pay:k1", "orders::o:2", "users::u:1"]
        await reset_users()
        assert sorted(backend.get_keys()) == ["idem:POST:/pay:k1", "orders::o:2"]

    async def test_before_invocation_evicts_before_the_method_runs(self) -> None:
        from pyfly.cache.decorators import cache_evict

        backend = InMemoryCache()
        await backend.put("k:1", "old")
        seen: list[bool] = []

        @cache_evict(backend, key="k:{n}", before_invocation=True)
        async def change(n: int) -> None:
            seen.append(await backend.exists(f"k:{n}"))

        await change(1)
        assert seen == [False]

    async def test_a_failing_put_or_eviction_never_fails_the_call(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        from pyfly.cache.decorators import cache_evict

        backend = _EvictionFails()
        calls: list[str] = []

        @cache_put(backend, key="k:{n}")
        async def write(n: int) -> str:
            calls.append("write")
            return "value"

        @cache_evict(backend, key="k:{n}")
        async def delete(n: int) -> str:
            calls.append("delete")
            return "deleted"

        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        assert await write(1) == "value"
        assert await delete(1) == "deleted"
        assert calls == ["write", "delete"]
        messages = [record.getMessage() for record in caplog.records]
        assert any(m.startswith("cache_put_skipped") and "ConnectionError" in m for m in messages)
        assert any(m.startswith("cache_evict_skipped") for m in messages)
        assert [r.levelname for r in caplog.records if r.getMessage().startswith("cache_evict")] == ["ERROR"]

    async def test_an_immediate_write_that_fails_follows_on_write_error(self, caplog: pytest.LogCaptureFixture) -> None:
        from pyfly.cache.decorators import cache_evict
        from pyfly.cache.transaction import TransactionAwareCache

        class _WritesFail(_EvictionFails):
            async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
                raise ConnectionError("cache server unreachable")

            async def clear(self) -> None:
                raise ConnectionError("cache server unreachable")

        backend = _WritesFail()
        strict = TransactionAwareCache(backend)
        with pytest.raises(ConnectionError):
            await strict.put_if_absent("k", 1)
        with pytest.raises(ConnectionError):
            await strict.evict_if_present("k")
        with pytest.raises(ConnectionError):
            await strict.invalidate()

        lenient = TransactionAwareCache(backend, on_write_error="log")
        caplog.set_level(logging.WARNING, logger="pyfly.cache")
        assert await lenient.put_if_absent("k", 1) is False
        assert await lenient.evict_if_present("k") is False
        await lenient.invalidate()
        messages = [record.getMessage() for record in caplog.records]
        assert [m.split(" ")[0] for m in messages] == [
            "cache_put_if_absent_skipped",
            "cache_evict_if_present_skipped",
            "cache_invalidate_skipped",
        ]

        # The decorators log: an eviction before invocation that fails no longer stops the method.
        calls: list[int] = []

        @cache_evict(backend, key="k:{n}", before_invocation=True)
        async def change(n: int) -> None:
            calls.append(n)

        await change(1)
        assert calls == [1]

    async def test_a_bad_key_template_fails_before_the_method_runs(self) -> None:
        calls: list[int] = []

        @cache_put(InMemoryCache(), key="x:{missing}")
        async def write(n: int) -> int:
            calls.append(n)
            return n

        with pytest.raises(ValueError, match="unknown parameter"):
            await write(1)
        assert calls == []


class TestNamespaces:
    """Clears are scoped to their cache; durable consumers get caches of their own (C034)."""

    async def test_a_dedicated_cache_is_disjoint_from_its_source(self) -> None:
        from pyfly.cache.namespaces import dedicated_cache

        root = InMemoryCache()
        idempotency = dedicated_cache(root, "idempotency")
        assert dedicated_cache(root, "idempotency") is idempotency
        await root.put("user:1", "evictable")
        await idempotency.put("idem:POST:/pay:k1", "durable")

        await root.clear()  # the admin's "evict all", @cache_evict(all_entries=True) on the root
        assert await root.exists("user:1") is False
        assert await idempotency.get("idem:POST:/pay:k1") == "durable"
        assert await root.exists("idem:POST:/pay:k1") is False

        await idempotency.clear()
        assert await idempotency.exists("idem:POST:/pay:k1") is False

    async def test_a_region_clears_its_prefix_only(self) -> None:
        from pyfly.cache.namespaces import cache_region

        root = InMemoryCache()
        users = cache_region(root, "users")
        await users.put("1", "alice")
        await root.put("orders::1", "order")
        assert await root.get("users::1") == "alice"
        assert await users.get_keys() == ["1"]
        await users.clear()
        assert root.get_keys() == ["orders::1"]

    async def test_orchestration_state_survives_clearing_the_cache(self) -> None:
        from datetime import UTC, datetime

        from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
        from pyfly.transactional.core.persistence import ExecutionState
        from pyfly.transactional.persistence.cache_adapter import CachePersistenceProvider

        root = InMemoryCache()
        provider = CachePersistenceProvider(root)
        now = datetime.now(UTC)
        state = ExecutionState(
            correlation_id="saga-42",
            name="payment",
            pattern=ExecutionPattern.SAGA,
            status=ExecutionStatus.RUNNING,
            started_at=now,
            updated_at=now,
            completed_at=None,
            payload={"correlation_id": "saga-42", "name": "payment", "pattern": "SAGA", "status": "RUNNING"},
        )
        await provider.save(state)
        await root.put("product:1", "cached")

        await root.clear()
        found = await CachePersistenceProvider(root).find("saga-42")
        assert found is not None
        assert found.status is ExecutionStatus.RUNNING
        assert [s.correlation_id for s in await provider.find_all()] == ["saga-42"]
        assert await root.exists("product:1") is False


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


class _Switchable(InMemoryCache):
    """A primary cache that can be taken down, as a Redis outage would."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("primary cache unreachable")

    async def get(self, key: str) -> Any | None:
        self._check()
        return await super().get(key)

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        self._check()
        await super().put(key, value, ttl)

    async def evict(self, key: str) -> bool:
        self._check()
        return await super().evict(key)

    async def exists(self, key: str) -> bool:
        self._check()
        return await super().exists(key)


class TestCacheManager:
    @pytest.mark.asyncio
    async def test_uses_primary(self):
        primary = InMemoryCache()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback)

        await manager.put("key", "value")
        assert await manager.get("key") == "value"
        # Not mirrored into the per-process fallback (C024): it would outlive evictions on other nodes.
        assert await fallback.exists("key") is False

    async def test_a_primary_miss_never_reads_the_fallback(self) -> None:
        primary = InMemoryCache()
        fallback = InMemoryCache()
        await fallback.put("price:1", "stale")
        manager = CacheManager(primary=primary, fallback=fallback)
        assert await manager.get("price:1") is None
        assert await manager.exists("price:1") is False

    async def test_an_eviction_on_one_node_reaches_every_node(self) -> None:
        """Three nodes share one primary, each with its own fallback (C024)."""
        shared = InMemoryCache()
        nodes = {name: CacheManager(primary=shared, fallback=InMemoryCache()) for name in "ABC"}
        db = {"price": 10}
        loads: list[str] = []

        def reader(node: str):  # noqa: ANN202
            @cacheable(nodes[node], key="price:1")
            async def get_price() -> int:
                loads.append(node)
                return db["price"]

            return get_price

        def writer(node: str):  # noqa: ANN202
            from pyfly.cache.decorators import cache_evict

            @cache_evict(nodes[node], key="price:1")
            async def set_price(value: int) -> None:
                db["price"] = value

            return set_price

        read = {name: reader(name) for name in "ABC"}
        assert await read["B"]() == 10
        await writer("A")(11)
        assert await read["C"]() == 11
        await writer("A")(12)
        assert [await read["B"](), await read["C"]()] == [12, 12]
        await writer("A")(13)
        for _ in range(3):
            assert [await read["B"](), await read["C"]()] == [13, 13]

    async def test_an_outage_uses_the_fallback_briefly_and_recovery_forgets_it(self) -> None:
        primary = _Switchable()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback, fallback_ttl=timedelta(seconds=30))
        await manager.put("k", "before")

        primary.down = True
        assert await manager.get("k") is None  # the outage starts cold: nothing was mirrored
        await manager.put("k", "during", ttl=timedelta(hours=1))
        assert await manager.get("k") == "during"
        assert fallback._store["k"][1] is not None  # capped by fallback_ttl, not the hour asked for

        primary.down = False
        assert await manager.get("k") == "before"
        assert await fallback.exists("k") is False  # recovery forgets what the outage wrote

    async def test_a_value_the_cache_refuses_is_not_an_outage(self) -> None:
        fallback = InMemoryCache()
        manager = CacheManager(primary=InMemoryCache(), fallback=fallback)
        with pytest.raises(CacheValueError, match="CachedProduct"):
            await manager.put("p", CachedProduct(id=1, name="widget"))
        assert fallback.get_keys() == []

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
