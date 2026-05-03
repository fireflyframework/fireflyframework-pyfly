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
"""Pagination types for paginated query results."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")


@dataclass(frozen=True)
class Page(Generic[T]):
    """A page of results from a paginated query.

    Attributes:
        items: The items on this page.
        total: Total number of items across all pages.
        page: Current page number (1-based).
        size: Maximum items per page.
    """

    items: list[T]
    total: int
    page: int
    size: int

    @property
    def total_pages(self) -> int:
        """Total number of pages."""
        if self.total == 0:
            return 0
        return math.ceil(self.total / self.size)

    @property
    def has_next(self) -> bool:
        """Whether there is a next page."""
        return self.page < self.total_pages

    @property
    def has_previous(self) -> bool:
        """Whether there is a previous page."""
        return self.page > 1

    def map(self, func: Callable[[T], U]) -> Page[U]:
        """Transform items using a mapping function, preserving pagination metadata."""
        return Page(
            items=[func(item) for item in self.items],
            total=self.total,
            page=self.page,
            size=self.size,
        )
