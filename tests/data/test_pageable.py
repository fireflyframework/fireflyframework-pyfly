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
"""Tests for Pageable and Sort types."""

from __future__ import annotations

import sys

import pytest

from pyfly.data.pageable import Order, Pageable, Sort

# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


class TestOrder:
    def test_asc_factory(self) -> None:
        order = Order.asc("name")
        assert order.property == "name"
        assert order.direction == "asc"

    def test_desc_factory(self) -> None:
        order = Order.desc("age")
        assert order.property == "age"
        assert order.direction == "desc"

    def test_default_direction_is_asc(self) -> None:
        order = Order(property="email")
        assert order.direction == "asc"

    def test_frozen(self) -> None:
        order = Order.asc("name")
        with pytest.raises(AttributeError):
            order.direction = "desc"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


class TestSort:
    def test_by_creates_ascending_sort(self) -> None:
        sort = Sort.by("name")
        assert len(sort.orders) == 1
        assert sort.orders[0].property == "name"
        assert sort.orders[0].direction == "asc"

    def test_by_multiple_properties(self) -> None:
        sort = Sort.by("name", "age", "email")
        assert len(sort.orders) == 3
        assert [o.property for o in sort.orders] == ["name", "age", "email"]
        assert all(o.direction == "asc" for o in sort.orders)

    def test_descending_flips_direction(self) -> None:
        sort = Sort.by("name").descending()
        assert sort.orders[0].direction == "desc"

    def test_ascending_flips_direction(self) -> None:
        sort = Sort.by("name").descending().ascending()
        assert sort.orders[0].direction == "asc"

    def test_and_then_combines_sorts(self) -> None:
        sort_a = Sort.by("name")
        sort_b = Sort.by("age").descending()
        combined = sort_a.and_then(sort_b)

        assert len(combined.orders) == 2
        assert combined.orders[0].property == "name"
        assert combined.orders[0].direction == "asc"
        assert combined.orders[1].property == "age"
        assert combined.orders[1].direction == "desc"

    def test_unsorted(self) -> None:
        sort = Sort.unsorted()
        assert sort.orders == ()

    def test_frozen(self) -> None:
        sort = Sort.by("name")
        with pytest.raises(AttributeError):
            sort.orders = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pageable
# ---------------------------------------------------------------------------


class TestPageable:
    def test_of_creates_pageable(self) -> None:
        pageable = Pageable.of(1, 20)
        assert pageable.page == 1
        assert pageable.size == 20
        assert pageable.sort.orders == ()

    def test_of_with_sort(self) -> None:
        sort = Sort.by("name")
        pageable = Pageable.of(2, 10, sort)
        assert pageable.page == 2
        assert pageable.size == 10
        assert pageable.sort is sort

    def test_offset_calculation(self) -> None:
        assert Pageable.of(1, 20).offset == 0
        assert Pageable.of(2, 20).offset == 20
        assert Pageable.of(3, 10).offset == 20
        assert Pageable.of(5, 25).offset == 100

    def test_next_page(self) -> None:
        pageable = Pageable.of(3, 10)
        next_page = pageable.next()
        assert next_page.page == 4
        assert next_page.size == 10

    def test_previous_page(self) -> None:
        pageable = Pageable.of(3, 10)
        prev_page = pageable.previous()
        assert prev_page.page == 2
        assert prev_page.size == 10

    def test_previous_page_min_is_one(self) -> None:
        pageable = Pageable.of(1, 10)
        prev_page = pageable.previous()
        assert prev_page.page == 1

    def test_next_preserves_sort(self) -> None:
        sort = Sort.by("name")
        pageable = Pageable.of(1, 10, sort)
        assert pageable.next().sort is sort

    def test_previous_preserves_sort(self) -> None:
        sort = Sort.by("name")
        pageable = Pageable.of(2, 10, sort)
        assert pageable.previous().sort is sort

    def test_unpaged(self) -> None:
        pageable = Pageable.unpaged()
        assert pageable.page == 1
        assert pageable.size == sys.maxsize
        assert pageable.is_paged is False

    def test_paged_is_paged(self) -> None:
        pageable = Pageable.of(1, 20)
        assert pageable.is_paged is True

    def test_default_values(self) -> None:
        pageable = Pageable()
        assert pageable.page == 1
        assert pageable.size == 20
        assert pageable.sort.orders == ()

    def test_rejects_page_less_than_one(self) -> None:
        with pytest.raises(ValueError, match="page must be >= 1"):
            Pageable.of(0, 20)

    def test_rejects_size_less_than_one(self) -> None:
        with pytest.raises(ValueError, match="size must be >= 1"):
            Pageable.of(1, 0)

    def test_frozen(self) -> None:
        pageable = Pageable.of(1, 20)
        with pytest.raises(AttributeError):
            pageable.page = 2  # type: ignore[misc]
