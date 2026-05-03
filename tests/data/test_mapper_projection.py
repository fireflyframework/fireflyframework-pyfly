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
"""Tests for Mapper.project() and register_projection()."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.data.mapper import Mapper


@dataclass
class OrderEntity:
    id: str
    customer: str
    quantity: int
    unit_price: float
    status: str


@dataclass
class OrderSummary:
    id: str
    status: str


@dataclass
class OrderWithTotal:
    id: str
    total: float


class TestProject:
    def test_project_subset_fields(self) -> None:
        order = OrderEntity(id="1", customer="alice", quantity=3, unit_price=10.0, status="shipped")
        mapper = Mapper()

        summary = mapper.project(order, OrderSummary)

        assert isinstance(summary, OrderSummary)
        assert summary.id == "1"
        assert summary.status == "shipped"

    def test_project_with_no_registration_uses_name_match(self) -> None:
        order = OrderEntity(id="2", customer="bob", quantity=1, unit_price=5.0, status="pending")
        mapper = Mapper()

        summary = mapper.project(order, OrderSummary)

        assert summary.id == "2"
        assert summary.status == "pending"


class TestRegisterProjection:
    def test_computed_field_via_transform(self) -> None:
        order = OrderEntity(id="1", customer="alice", quantity=3, unit_price=10.0, status="shipped")
        mapper = Mapper()
        mapper.register_projection(
            OrderEntity,
            OrderWithTotal,
            transforms={"total": lambda o: o.quantity * o.unit_price},
        )

        result = mapper.project(order, OrderWithTotal)

        assert result.id == "1"
        assert result.total == 30.0

    def test_transform_overrides_source_field(self) -> None:
        order = OrderEntity(id="1", customer="alice", quantity=2, unit_price=7.5, status="shipped")
        mapper = Mapper()
        mapper.register_projection(
            OrderEntity,
            OrderSummary,
            transforms={"status": lambda o: o.status.upper()},
        )

        result = mapper.project(order, OrderSummary)

        assert result.id == "1"
        assert result.status == "SHIPPED"


class TestProjectWithPageMap:
    def test_page_map_with_project(self) -> None:
        from pyfly.data.page import Page

        orders = [
            OrderEntity(id="1", customer="a", quantity=1, unit_price=10.0, status="shipped"),
            OrderEntity(id="2", customer="b", quantity=2, unit_price=20.0, status="pending"),
        ]
        page = Page(items=orders, total=2, page=1, size=10)
        mapper = Mapper()

        summary_page = page.map(lambda e: mapper.project(e, OrderSummary))

        assert len(summary_page.items) == 2
        assert all(isinstance(s, OrderSummary) for s in summary_page.items)
        assert summary_page.items[0].status == "shipped"
        assert summary_page.total == 2
