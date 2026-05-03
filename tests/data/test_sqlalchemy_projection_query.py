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
"""Tests for SQLAlchemy query compiler projection support."""

from __future__ import annotations

from typing import Protocol

import pytest
from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pyfly.data.projection import projection, projection_fields


class Base(DeclarativeBase):
    pass


class OrderModel(Base):
    __tablename__ = "test_proj_orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer: Mapped[str] = mapped_column(String)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String)


@projection
class OrderSummaryProjection(Protocol):
    id: str
    status: str


class TestProjectionQueryCompilation:
    def test_projection_fields_match_model_columns(self) -> None:
        fields = projection_fields(OrderSummaryProjection)
        for f in fields:
            assert hasattr(OrderModel, f), f"OrderModel missing column '{f}'"

    @pytest.mark.asyncio
    async def test_projected_select_returns_subset(self) -> None:
        from sqlalchemy import select

        fields = projection_fields(OrderSummaryProjection)
        columns = [getattr(OrderModel, f) for f in fields]
        stmt = select(*columns)

        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "test_proj_orders.id" in compiled
        assert "test_proj_orders.status" in compiled
        assert "test_proj_orders.customer" not in compiled
        assert "test_proj_orders.quantity" not in compiled

    def test_compiler_accepts_return_type(self) -> None:
        from pyfly.data.query_parser import QueryMethodParser
        from pyfly.data.relational.sqlalchemy.query_compiler import QueryMethodCompiler

        parser = QueryMethodParser()
        parsed = parser.parse("find_by_status")
        compiler = QueryMethodCompiler()

        # Should not raise with return_type parameter
        fn = compiler.compile(parsed, OrderModel, return_type=list[OrderSummaryProjection])
        assert callable(fn)

    def test_compiler_works_without_return_type(self) -> None:
        from pyfly.data.query_parser import QueryMethodParser
        from pyfly.data.relational.sqlalchemy.query_compiler import QueryMethodCompiler

        parser = QueryMethodParser()
        parsed = parser.parse("find_by_status")
        compiler = QueryMethodCompiler()

        # Should work as before without return_type
        fn = compiler.compile(parsed, OrderModel)
        assert callable(fn)
