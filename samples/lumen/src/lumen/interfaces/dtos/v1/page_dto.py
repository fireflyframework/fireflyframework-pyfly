# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""A JSON-friendly page envelope for paginated list endpoints.

The framework returns query results as a :class:`pyfly.data.Page` — items
plus pagination metadata (total, page, size, total_pages, has_next/…).
:class:`PageDto` is the public, serialisable mirror of that, built with
:meth:`from_page` so the list endpoints can return the page over the wire.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

from pyfly.data import Page

T = TypeVar("T")


class PageDto(BaseModel, Generic[T]):
    """A page of ``T`` plus pagination metadata."""

    items: list[T]
    total: int
    page: int
    size: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @classmethod
    def from_page(cls, page: Page[T]) -> PageDto[T]:
        """Build the wire envelope from a framework :class:`Page`."""
        return cls(
            items=page.items,
            total=page.total,
            page=page.page,
            size=page.size,
            total_pages=page.total_pages,
            has_next=page.has_next,
            has_previous=page.has_previous,
        )
