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
"""Spring-like Pageable and Sort types for pagination requests.

An :class:`Order` is a property, a direction, a :class:`NullHandling` and an ``ignore_case`` flag, so the same
``Pageable`` returns the same page on every backend: NULL placement differs by database (PostgreSQL and
Oracle put NULLs last in ascending order, SQLite, MySQL, MariaDB, SQL Server and MongoDB first), so an order
that pages over a nullable property should name it (``Order.asc("score").nulls_last()``). ``ignore_case``
compares lower-cased values of a string property (any other property is compared as it is). Collation
(accents, the order of upper and lower case) and the order of enum values still follow the database:
PostgreSQL, MySQL and MariaDB order a native enum by its declaration, SQLite and MongoDB by its text.

:class:`KeysetPosition` is where a keyset scroll resumes (``Repository.scroll``): the values of the sort
properties (and the primary key) of the last row read.
"""

from __future__ import annotations

import dataclasses
import enum
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


class NullHandling(enum.Enum):
    """Where an :class:`Order` puts NULL values (Spring's ``Sort.NullHandling``)."""

    NATIVE = "NATIVE"
    """Whatever the database does (see the module documentation: it differs by backend)."""
    NULLS_FIRST = "NULLS_FIRST"
    """NULL values before every other value, whatever the direction."""
    NULLS_LAST = "NULLS_LAST"
    """NULL values after every other value, whatever the direction."""


@dataclass(frozen=True)
class Order:
    """A single sort order: property name, direction, NULL placement and case sensitivity."""

    property: str
    direction: Literal["asc", "desc"] = "asc"
    null_handling: NullHandling = NullHandling.NATIVE
    ignore_case: bool = False

    @staticmethod
    def asc(property: str) -> Order:
        """Create an ascending order for the given property."""
        return Order(property=property, direction="asc")

    @staticmethod
    def desc(property: str) -> Order:
        """Create a descending order for the given property."""
        return Order(property=property, direction="desc")

    def with_null_handling(self, null_handling: NullHandling) -> Order:
        """This order with *null_handling*."""
        return dataclasses.replace(self, null_handling=null_handling)

    def nulls_first(self) -> Order:
        """This order with NULL values first."""
        return self.with_null_handling(NullHandling.NULLS_FIRST)

    def nulls_last(self) -> Order:
        """This order with NULL values last."""
        return self.with_null_handling(NullHandling.NULLS_LAST)

    def nulls_native(self) -> Order:
        """This order with the database's own NULL placement."""
        return self.with_null_handling(NullHandling.NATIVE)

    def ignoring_case(self) -> Order:
        """This order comparing lower-cased values (of a string property; others compare as they are)."""
        return dataclasses.replace(self, ignore_case=True)


@dataclass(frozen=True)
class Sort:
    """Collection of sort orders."""

    orders: tuple[Order, ...] = ()

    @staticmethod
    def by(*properties: str | Order) -> Sort:
        """Create a sort from property names (ascending) and :class:`Order` objects, in that order."""
        return Sort(orders=tuple(item if isinstance(item, Order) else Order.asc(item) for item in properties))

    @staticmethod
    def unsorted() -> Sort:
        """No sorting."""
        return Sort()

    @property
    def is_sorted(self) -> bool:
        """Whether this sort has at least one order."""
        return bool(self.orders)

    def and_then(self, other: Sort) -> Sort:
        """Combine sorts, appending *other*'s orders after this sort's orders."""
        return Sort(orders=self.orders + other.orders)

    def descending(self) -> Sort:
        """Return same sort but all directions flipped to desc (NULL handling and case kept)."""
        return Sort(orders=tuple(dataclasses.replace(o, direction="desc") for o in self.orders))

    def ascending(self) -> Sort:
        """Return same sort but all directions flipped to asc (NULL handling and case kept)."""
        return Sort(orders=tuple(dataclasses.replace(o, direction="asc") for o in self.orders))


_UNPAGED_SENTINEL_SIZE = sys.maxsize


@dataclass(frozen=True)
class Pageable:
    """Pagination request: page number, size, and sort criteria."""

    page: int = 1
    size: int = 20
    sort: Sort = field(default_factory=Sort)

    def __post_init__(self) -> None:
        if self.size != _UNPAGED_SENTINEL_SIZE:
            if self.page < 1:
                raise ValueError(f"page must be >= 1, got {self.page}")
            if self.size < 1:
                raise ValueError(f"size must be >= 1, got {self.size}")

    @staticmethod
    def of(page: int, size: int, sort: Sort | None = None) -> Pageable:
        """Create a pageable for the given page, size, and optional sort."""
        return Pageable(page=page, size=size, sort=sort or Sort())

    @staticmethod
    def unpaged() -> Pageable:
        """No pagination (fetch all)."""
        return Pageable(page=1, size=_UNPAGED_SENTINEL_SIZE, sort=Sort())

    @property
    def is_paged(self) -> bool:
        """Whether this pageable represents actual pagination."""
        return self.size != _UNPAGED_SENTINEL_SIZE

    @property
    def offset(self) -> int:
        """Calculate the pagination offset."""
        return (self.page - 1) * self.size

    def next(self) -> Pageable:
        """Return Pageable for next page."""
        return Pageable(page=self.page + 1, size=self.size, sort=self.sort)

    def previous(self) -> Pageable:
        """Return Pageable for previous page (min page 1)."""
        return Pageable(page=max(1, self.page - 1), size=self.size, sort=self.sort)


@dataclass(frozen=True)
class KeysetPosition:
    """Where a keyset scroll resumes: the sort-property (and primary-key) values of the last row read.

    A :class:`~pyfly.data.page.Window` gives the position after its last item (``window.next_position``);
    pass it back to continue. ``keys`` maps property names to values, so a web API can serialize it into a
    cursor token and build it again with :meth:`of`. A position is a value: it holds its own copy of the keys
    (a plain ``dict``, so ``dataclasses.asdict`` and JSON serialization see a mapping), and equal positions hash
    alike, so one can be a cache key; do not change its keys.
    """

    keys: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", dict(self.keys))

    def __hash__(self) -> int:
        return hash(frozenset(self.keys.items()))

    @staticmethod
    def of(**keys: Any) -> KeysetPosition:
        """A position from property values (``KeysetPosition.of(name="m", id=42)``)."""
        return KeysetPosition(keys=dict(keys))
