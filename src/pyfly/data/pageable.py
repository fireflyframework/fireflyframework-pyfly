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
"""Spring-like Pageable and Sort types for pagination requests."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Order:
    """A single sort order: property name + direction."""

    property: str
    direction: Literal["asc", "desc"] = "asc"

    @staticmethod
    def asc(property: str) -> Order:
        """Create an ascending order for the given property."""
        return Order(property=property, direction="asc")

    @staticmethod
    def desc(property: str) -> Order:
        """Create a descending order for the given property."""
        return Order(property=property, direction="desc")


@dataclass(frozen=True)
class Sort:
    """Collection of sort orders."""

    orders: tuple[Order, ...] = ()

    @staticmethod
    def by(*properties: str) -> Sort:
        """Create ascending sort by properties."""
        return Sort(orders=tuple(Order.asc(p) for p in properties))

    @staticmethod
    def unsorted() -> Sort:
        """No sorting."""
        return Sort()

    def and_then(self, other: Sort) -> Sort:
        """Combine sorts, appending *other*'s orders after this sort's orders."""
        return Sort(orders=self.orders + other.orders)

    def descending(self) -> Sort:
        """Return same sort but all directions flipped to desc."""
        return Sort(orders=tuple(Order(property=o.property, direction="desc") for o in self.orders))

    def ascending(self) -> Sort:
        """Return same sort but all directions flipped to asc."""
        return Sort(orders=tuple(Order(property=o.property, direction="asc") for o in self.orders))


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
