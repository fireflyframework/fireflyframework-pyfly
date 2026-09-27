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

from pyfly.data.page import Page, Slice, Window
from pyfly.data.pageable import KeysetPosition, NullHandling, Order, Pageable, Sort

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


# ---------------------------------------------------------------------------
# NULL handling, case and keyset positions (WP03-17, C112)
# ---------------------------------------------------------------------------


class TestNullHandlingAndCase:
    def test_an_order_defaults_to_native_nulls_and_case_sensitive(self) -> None:
        order = Order.asc("score")
        assert order.null_handling is NullHandling.NATIVE
        assert order.ignore_case is False

    def test_null_placement_and_case_are_set_without_touching_the_rest(self) -> None:
        order = Order.desc("score").nulls_last().ignoring_case()
        assert (order.property, order.direction) == ("score", "desc")
        assert order.null_handling is NullHandling.NULLS_LAST
        assert order.ignore_case is True
        assert order.nulls_first().null_handling is NullHandling.NULLS_FIRST
        assert order.nulls_native().null_handling is NullHandling.NATIVE
        assert order.with_null_handling(NullHandling.NULLS_FIRST) == order.nulls_first()

    def test_sort_by_takes_orders_and_names(self) -> None:
        sort = Sort.by(Order.desc("score").nulls_last(), "name")
        assert sort.orders == (Order.desc("score").nulls_last(), Order.asc("name"))
        assert sort.is_sorted
        assert not Sort.unsorted().is_sorted

    def test_flipping_the_direction_keeps_null_handling_and_case(self) -> None:
        sort = Sort.by(Order.asc("score").nulls_first().ignoring_case())
        flipped = sort.descending().orders[0]
        assert flipped.direction == "desc"
        assert flipped.null_handling is NullHandling.NULLS_FIRST
        assert flipped.ignore_case is True
        assert sort.descending().ascending() == sort


class TestKeysetPosition:
    def test_of_builds_the_keys(self) -> None:
        position = KeysetPosition.of(name="m", id=42)
        assert dict(position.keys) == {"name": "m", "id": 42}
        assert KeysetPosition().keys == {}


# ---------------------------------------------------------------------------
# Page, Slice and Window (WP03-10, C135)
# ---------------------------------------------------------------------------


class TestPageSliceWindow:
    def test_a_slice_knows_its_neighbours_without_a_total(self) -> None:
        first = Slice(items=[1, 2], page=1, size=2, has_next=True)
        assert first.has_next and not first.has_previous
        assert first.is_first and not first.is_last
        last = Slice(items=[3], page=2, size=2, has_next=False)
        assert last.has_previous and last.is_last
        assert last.map(str) == Slice(items=["3"], page=2, size=2, has_next=False)

    def test_a_window_resumes_after_its_last_item(self) -> None:
        window = Window(items=["a", "b"], has_next=True, next_position=KeysetPosition.of(name="b", id=2))
        assert window.next_position == KeysetPosition.of(name="b", id=2)
        assert window.map(str.upper).items == ["A", "B"]
        assert window.map(str.upper).next_position == window.next_position
        assert Window(items=[], has_next=False, next_position=None).is_last

    def test_page_map_keeps_the_total(self) -> None:
        page = Page(items=[1, 2], total=5, page=1, size=2)
        assert page.map(str) == Page(items=["1", "2"], total=5, page=1, size=2)
        assert page.total_pages == 3
