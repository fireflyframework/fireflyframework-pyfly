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
"""Fluent builder for creating and executing queries.

Mirrors Java's ``QueryBuilder`` — reduces boilerplate when constructing
queries and dispatching them through the bus.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pyfly.cqrs.types import Query

Q = TypeVar("Q", bound=Query)  # type: ignore[type-arg]
R = TypeVar("R")


class QueryBuilder(Generic[Q, R]):
    """Fluent builder for query creation and optional execution.

    Usage::

        order = await (
            QueryBuilder.create(GetOrderQuery)
            .with_field("order_id", "ord-123")
            .cached(True)
            .execute_with(query_bus)
        )
    """

    def __init__(self, query_type: type[Q]) -> None:
        self._query_type = query_type
        self._fields: dict[str, Any] = {}
        self._correlation_id: str | None = None
        self._cacheable: bool | None = None
        self._cache_key: str | None = None
        self._timestamp: datetime | None = None
        self._metadata: dict[str, Any] = {}

    @staticmethod
    def create(query_type: type[Q]) -> QueryBuilder[Q, R]:
        return QueryBuilder(query_type)

    # ── domain fields ──────────────────────────────────────────

    def with_field(self, name: str, value: Any) -> QueryBuilder[Q, R]:
        self._fields[name] = value
        return self

    def with_fields(self, **kwargs: Any) -> QueryBuilder[Q, R]:
        self._fields.update(kwargs)
        return self

    # ── metadata ───────────────────────────────────────────────

    def correlated_by(self, correlation_id: str) -> QueryBuilder[Q, R]:
        self._correlation_id = correlation_id
        return self

    def at(self, timestamp: datetime) -> QueryBuilder[Q, R]:
        self._timestamp = timestamp
        return self

    def with_metadata(self, key: str, value: Any) -> QueryBuilder[Q, R]:
        self._metadata[key] = value
        return self

    # ── caching ────────────────────────────────────────────────

    def cached(self, enabled: bool = True) -> QueryBuilder[Q, R]:
        self._cacheable = enabled
        return self

    def with_cache_key(self, key: str) -> QueryBuilder[Q, R]:
        self._cache_key = key
        return self

    # ── build ──────────────────────────────────────────────────

    def build(self) -> Q:
        """Construct the query instance.

        Domain-specific fields are passed as constructor kwargs.
        Metadata is set via the Query method API afterward.
        """
        query = self._query_type(**self._fields)
        if self._correlation_id:
            query.set_correlation_id(self._correlation_id)
        if self._cacheable is not None:
            query.set_cacheable(self._cacheable)
        if self._cache_key is not None:

            def _override_cache_key() -> str:
                return self._cache_key  # type: ignore[return-value]

            object.__setattr__(query, "get_cache_key", _override_cache_key)
        if self._timestamp is not None:
            object.__setattr__(query, "_cqrs_timestamp", self._timestamp)
        for k, v in self._metadata.items():
            query.get_metadata()[k] = v
        return query

    # ── build + execute ────────────────────────────────────────

    async def execute_with(self, bus: Any) -> Any:
        """Build the query and dispatch it through a :class:`QueryBus`."""
        query = self.build()
        return await bus.query(query)
