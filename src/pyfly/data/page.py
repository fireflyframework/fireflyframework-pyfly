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
"""Result types of paginated queries.

- :class:`Page`: a page and the total number of items (``find_all(Pageable)``). The repositories skip
  the ``COUNT`` when the page itself gives the total: a first page shorter than its size, or any short page
  after it (Spring's ``PageableExecutionUtils`` rule).
- :class:`Slice`: a page and whether another one follows, with no ``COUNT`` at all
  (``find_slice(Pageable)``: one query with ``LIMIT size + 1``).
- :class:`Window`: the rows after a :class:`~pyfly.data.pageable.KeysetPosition` (``scroll``), whose cost
  does not grow with the depth of the page, as an ``OFFSET`` does.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pyfly.data.pageable import KeysetPosition

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


@dataclass(frozen=True)
class Slice(Generic[T]):
    """A page of results that knows whether another page follows, but not the total (Spring's ``Slice``).

    Attributes:
        items: The items on this page.
        page: Current page number (1-based).
        size: Maximum items per page.
        has_next: Whether at least one more item follows this page.
    """

    items: list[T]
    page: int
    size: int
    has_next: bool

    @property
    def has_previous(self) -> bool:
        """Whether there is a previous page."""
        return self.page > 1

    @property
    def is_first(self) -> bool:
        """Whether this is the first page."""
        return not self.has_previous

    @property
    def is_last(self) -> bool:
        """Whether no page follows this one."""
        return not self.has_next

    def map(self, func: Callable[[T], U]) -> Slice[U]:
        """Transform items using a mapping function, preserving the slice metadata."""
        return Slice(items=[func(item) for item in self.items], page=self.page, size=self.size, has_next=self.has_next)


@dataclass(frozen=True)
class Window(Generic[T]):
    """The rows of a keyset scroll (Spring's ``Window``).

    Attributes:
        items: The items of this window, in the scroll's order.
        has_next: Whether at least one more item follows this window.
        next_position: Where the next window starts (the keys of the last item), or ``None`` when the window
            is empty.
    """

    items: list[T]
    has_next: bool
    next_position: KeysetPosition | None

    @property
    def is_last(self) -> bool:
        """Whether no item follows this window."""
        return not self.has_next

    def map(self, func: Callable[[T], U]) -> Window[U]:
        """Transform items using a mapping function, preserving the scroll position."""
        return Window(
            items=[func(item) for item in self.items], has_next=self.has_next, next_position=self.next_position
        )
