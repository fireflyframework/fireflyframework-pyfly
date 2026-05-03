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
"""Tests for CommandBuilder and QueryBuilder fluent APIs."""

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from pyfly.cqrs.fluent.command_builder import CommandBuilder
from pyfly.cqrs.fluent.query_builder import QueryBuilder
from pyfly.cqrs.types import Command, Query

# -- Test messages ----------------------------------------------------------


@dataclass
class CreateOrderCommand(Command[str]):
    customer_id: str = ""
    amount: float = 0.0


@dataclass
class GetOrderQuery(Query[dict]):
    order_id: str = ""
    include_details: bool = False


# -- CommandBuilder tests ---------------------------------------------------


class TestCommandBuilder:
    def test_create_and_build_with_field(self) -> None:
        cmd = (
            CommandBuilder.create(CreateOrderCommand)
            .with_field("customer_id", "cust-1")
            .with_field("amount", 99.99)
            .build()
        )
        assert isinstance(cmd, CreateOrderCommand)
        assert cmd.customer_id == "cust-1"
        assert cmd.amount == 99.99

    def test_with_fields_kwargs(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_fields(customer_id="cust-2", amount=50.0).build()
        assert cmd.customer_id == "cust-2"
        assert cmd.amount == 50.0

    def test_correlated_by_sets_correlation_id(self) -> None:
        cmd = (
            CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").correlated_by("corr-abc").build()
        )
        assert cmd.get_correlation_id() == "corr-abc"

    def test_initiated_by_sets_user(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").initiated_by("user-42").build()
        assert cmd.get_initiated_by() == "user-42"

    def test_with_metadata_adds_entries(self) -> None:
        cmd = (
            CommandBuilder.create(CreateOrderCommand)
            .with_field("customer_id", "c1")
            .with_metadata("source", "api")
            .with_metadata("version", "v2")
            .build()
        )
        metadata = cmd.get_metadata()
        assert metadata["source"] == "api"
        assert metadata["version"] == "v2"

    def test_build_assigns_command_id(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").build()
        command_id = cmd.get_command_id()
        assert command_id is not None
        assert len(command_id) == 36

    def test_build_assigns_default_timestamp(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").build()
        ts = cmd.get_timestamp()
        assert ts.tzinfo is not None
        now = datetime.now(UTC)
        assert (now - ts).total_seconds() < 2

    def test_at_sets_timestamp(self) -> None:
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").at(ts).build()
        assert cmd.get_timestamp() == ts

    async def test_execute_with_builds_and_sends(self) -> None:
        mock_bus = AsyncMock()
        mock_bus.send.return_value = "order-123"

        result = await (
            CommandBuilder.create(CreateOrderCommand)
            .with_field("customer_id", "cust-exec")
            .correlated_by("corr-exec")
            .execute_with(mock_bus)
        )

        assert result == "order-123"
        mock_bus.send.assert_called_once()
        sent_cmd = mock_bus.send.call_args[0][0]
        assert isinstance(sent_cmd, CreateOrderCommand)
        assert sent_cmd.customer_id == "cust-exec"
        assert sent_cmd.get_correlation_id() == "corr-exec"

    def test_chaining_returns_same_builder(self) -> None:
        builder = CommandBuilder.create(CreateOrderCommand)
        assert builder.with_field("customer_id", "c1") is builder
        assert builder.with_fields(amount=10) is builder
        assert builder.correlated_by("corr") is builder
        assert builder.initiated_by("user") is builder
        assert builder.with_metadata("k", "v") is builder

    def test_no_correlation_id_when_not_set(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").build()
        assert cmd.get_correlation_id() is None

    def test_no_initiated_by_when_not_set(self) -> None:
        cmd = CommandBuilder.create(CreateOrderCommand).with_field("customer_id", "c1").build()
        assert cmd.get_initiated_by() is None


# -- QueryBuilder tests -----------------------------------------------------


class TestQueryBuilder:
    def test_create_and_build_with_field(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "ord-1").build()
        assert isinstance(query, GetOrderQuery)
        assert query.order_id == "ord-1"

    def test_with_fields_kwargs(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_fields(order_id="ord-2", include_details=True).build()
        assert query.order_id == "ord-2"
        assert query.include_details is True

    def test_cached_sets_cacheable(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").cached(True).build()
        assert query.is_cacheable() is True

    def test_cached_false_disables_caching(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").cached(False).build()
        assert query.is_cacheable() is False

    def test_correlated_by_sets_correlation_id(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").correlated_by("corr-q1").build()
        assert query.get_correlation_id() == "corr-q1"

    def test_with_metadata_adds_entries(self) -> None:
        query = (
            QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").with_metadata("source", "dashboard").build()
        )
        assert query.get_metadata()["source"] == "dashboard"

    def test_build_assigns_query_id(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").build()
        query_id = query.get_query_id()
        assert query_id is not None
        assert len(query_id) == 36

    async def test_execute_with_builds_and_queries(self) -> None:
        mock_bus = AsyncMock()
        mock_bus.query.return_value = {"id": "ord-exec", "status": "pending"}

        result = await (
            QueryBuilder.create(GetOrderQuery).with_field("order_id", "ord-exec").cached(True).execute_with(mock_bus)
        )

        assert result == {"id": "ord-exec", "status": "pending"}
        mock_bus.query.assert_called_once()
        sent_query = mock_bus.query.call_args[0][0]
        assert isinstance(sent_query, GetOrderQuery)
        assert sent_query.order_id == "ord-exec"
        assert sent_query.is_cacheable() is True

    def test_chaining_returns_same_builder(self) -> None:
        builder = QueryBuilder.create(GetOrderQuery)
        assert builder.with_field("order_id", "o1") is builder
        assert builder.with_fields(include_details=True) is builder
        assert builder.correlated_by("corr") is builder
        assert builder.cached(True) is builder
        assert builder.with_metadata("k", "v") is builder

    def test_with_cache_key(self) -> None:
        builder = QueryBuilder.create(GetOrderQuery)
        result = builder.with_cache_key("my-key")
        assert result is builder

    def test_with_cache_key_overrides_get_cache_key(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").with_cache_key("custom-key").build()
        assert query.get_cache_key() == "custom-key"

    def test_at_sets_timestamp(self) -> None:
        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").at(ts).build()
        assert query.get_timestamp() == ts

    def test_default_cacheable_is_true(self) -> None:
        query = QueryBuilder.create(GetOrderQuery).with_field("order_id", "o1").build()
        assert query.is_cacheable() is True
