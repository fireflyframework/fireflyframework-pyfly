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
"""Tests for CQRS base types — Command and Query."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest

from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.types import Command, Query
from pyfly.cqrs.validation.types import ValidationResult

# ── test command / query subclasses ───────────────────────────


@dataclass(frozen=True)
class CreateOrderCommand(Command[str]):
    customer_id: str = "cust-1"
    amount: float = 99.99


@dataclass(frozen=True)
class DeleteOrderCommand(Command[None]):
    order_id: str = "order-1"


@dataclass(frozen=True)
class GetOrderQuery(Query[dict]):
    order_id: str = "order-1"


@dataclass(frozen=True)
class ListOrdersQuery(Query[list[dict]]):
    customer_id: str = "cust-1"


# ── subclass with custom validate / authorize ─────────────────


@dataclass(frozen=True)
class InvalidCommand(Command[None]):
    value: str = ""

    async def validate(self) -> ValidationResult:
        if not self.value:
            return ValidationResult.failure("value", "value must not be empty")
        return ValidationResult.success()

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.failure(
            resource="orders",
            message="not allowed",
            denied_action="create",
        )


@dataclass(frozen=True)
class ValidatedQuery(Query[str]):
    keyword: str = ""

    async def validate(self) -> ValidationResult:
        if len(self.keyword) < 3:
            return ValidationResult.failure("keyword", "keyword too short")
        return ValidationResult.success()

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.failure(
            resource="search",
            message="search not permitted",
        )


# ── Command tests ─────────────────────────────────────────────


class TestCommand:
    def test_command_id_is_valid_uuid(self) -> None:
        cmd = CreateOrderCommand()
        UUID(cmd.get_command_id())

    def test_command_id_is_unique_per_instance(self) -> None:
        cmd1 = CreateOrderCommand()
        cmd2 = CreateOrderCommand()
        assert cmd1.get_command_id() != cmd2.get_command_id()

    def test_correlation_id_defaults_to_none(self) -> None:
        cmd = CreateOrderCommand()
        assert cmd.get_correlation_id() is None

    def test_correlation_id_can_be_set(self) -> None:
        cmd = CreateOrderCommand()
        cmd.set_correlation_id("corr-123")
        assert cmd.get_correlation_id() == "corr-123"

    def test_timestamp_is_utc_datetime(self) -> None:
        cmd = CreateOrderCommand()
        assert isinstance(cmd.get_timestamp(), datetime)
        assert cmd.get_timestamp().tzinfo is not None

    def test_timestamp_is_recent(self) -> None:
        before = datetime.now(UTC)
        cmd = CreateOrderCommand()
        ts = cmd.get_timestamp()  # lazy init happens here
        after = datetime.now(UTC)
        assert before <= ts <= after

    def test_initiated_by_defaults_to_none(self) -> None:
        cmd = CreateOrderCommand()
        assert cmd.get_initiated_by() is None

    def test_initiated_by_can_be_set(self) -> None:
        cmd = CreateOrderCommand()
        cmd.set_initiated_by("user-42")
        assert cmd.get_initiated_by() == "user-42"

    def test_metadata_defaults_to_empty_dict(self) -> None:
        cmd = CreateOrderCommand()
        assert cmd.get_metadata() == {}

    def test_metadata_can_be_set(self) -> None:
        cmd = CreateOrderCommand()
        cmd.set_metadata("source", "api")
        assert cmd.get_metadata() == {"source": "api"}

    def test_get_cache_key_returns_none_by_default(self) -> None:
        cmd = CreateOrderCommand()
        assert cmd.get_cache_key() is None

    @pytest.mark.asyncio
    async def test_validate_returns_success_by_default(self) -> None:
        cmd = CreateOrderCommand()
        result = await cmd.validate()
        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == ()

    @pytest.mark.asyncio
    async def test_authorize_returns_success_by_default(self) -> None:
        cmd = CreateOrderCommand()
        result = await cmd.authorize()
        assert isinstance(result, AuthorizationResult)
        assert result.authorized is True
        assert result.errors == ()

    @pytest.mark.asyncio
    async def test_authorize_with_context_delegates_to_authorize(self) -> None:
        cmd = CreateOrderCommand()
        result = await cmd.authorize_with_context(None)
        assert result.authorized is True

    def test_domain_fields_preserved(self) -> None:
        cmd = CreateOrderCommand(customer_id="cust-99", amount=50.0)
        assert cmd.customer_id == "cust-99"
        assert cmd.amount == 50.0

    def test_generic_type_parameter(self) -> None:
        assert Command.__class_getitem__ is not None
        cmd: Command[str] = CreateOrderCommand()
        assert isinstance(cmd, Command)


# ── Query tests ───────────────────────────────────────────────


class TestQuery:
    def test_query_id_is_valid_uuid(self) -> None:
        query = GetOrderQuery()
        UUID(query.get_query_id())

    def test_query_id_is_unique_per_instance(self) -> None:
        q1 = GetOrderQuery()
        q2 = GetOrderQuery()
        assert q1.get_query_id() != q2.get_query_id()

    def test_correlation_id_defaults_to_none(self) -> None:
        query = GetOrderQuery()
        assert query.get_correlation_id() is None

    def test_correlation_id_can_be_set(self) -> None:
        query = GetOrderQuery()
        query.set_correlation_id("corr-456")
        assert query.get_correlation_id() == "corr-456"

    def test_timestamp_is_utc_datetime(self) -> None:
        query = GetOrderQuery()
        assert isinstance(query.get_timestamp(), datetime)
        assert query.get_timestamp().tzinfo is not None

    def test_metadata_defaults_to_empty_dict(self) -> None:
        query = GetOrderQuery()
        assert query.get_metadata() == {}

    def test_cacheable_defaults_to_true(self) -> None:
        query = GetOrderQuery()
        assert query.is_cacheable() is True

    def test_cacheable_can_be_set_to_false(self) -> None:
        query = GetOrderQuery()
        query.set_cacheable(False)
        assert query.is_cacheable() is False

    def test_get_cache_key_includes_class_name(self) -> None:
        query = GetOrderQuery()
        assert query.get_cache_key().startswith("GetOrderQuery:")

    def test_get_cache_key_differs_by_subclass(self) -> None:
        q1 = GetOrderQuery()
        q2 = ListOrdersQuery()
        assert q1.get_cache_key() != q2.get_cache_key()
        assert q2.get_cache_key().startswith("ListOrdersQuery:")

    def test_get_cache_key_differs_by_field_values(self) -> None:
        q1 = GetOrderQuery(order_id="order-1")
        q2 = GetOrderQuery(order_id="order-2")
        assert q1.get_cache_key() != q2.get_cache_key()

    def test_get_cache_key_same_for_same_fields(self) -> None:
        q1 = GetOrderQuery(order_id="order-1")
        q2 = GetOrderQuery(order_id="order-1")
        assert q1.get_cache_key() == q2.get_cache_key()

    @pytest.mark.asyncio
    async def test_validate_returns_success_by_default(self) -> None:
        query = GetOrderQuery()
        result = await query.validate()
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_authorize_returns_success_by_default(self) -> None:
        query = GetOrderQuery()
        result = await query.authorize()
        assert result.authorized is True

    @pytest.mark.asyncio
    async def test_authorize_with_context_delegates_to_authorize(self) -> None:
        query = GetOrderQuery()
        result = await query.authorize_with_context(None)
        assert result.authorized is True

    def test_domain_fields_preserved(self) -> None:
        query = GetOrderQuery(order_id="order-42")
        assert query.order_id == "order-42"

    def test_generic_type_parameter(self) -> None:
        query: Query[dict] = GetOrderQuery()
        assert isinstance(query, Query)


# ── custom validate / authorize subclass tests ────────────────


class TestCustomValidateAndAuthorize:
    @pytest.mark.asyncio
    async def test_command_validate_returns_failure(self) -> None:
        cmd = InvalidCommand()
        result = await cmd.validate()
        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].field_name == "value"
        assert "empty" in result.errors[0].message

    @pytest.mark.asyncio
    async def test_command_validate_returns_success_when_valid(self) -> None:
        cmd = InvalidCommand(value="ok")
        result = await cmd.validate()
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_command_authorize_returns_failure(self) -> None:
        cmd = InvalidCommand()
        result = await cmd.authorize()
        assert result.authorized is False
        assert len(result.errors) == 1
        assert result.errors[0].resource == "orders"
        assert result.errors[0].denied_action == "create"

    @pytest.mark.asyncio
    async def test_query_validate_returns_failure(self) -> None:
        query = ValidatedQuery(keyword="ab")
        result = await query.validate()
        assert result.valid is False
        assert result.errors[0].field_name == "keyword"

    @pytest.mark.asyncio
    async def test_query_validate_returns_success_when_valid(self) -> None:
        query = ValidatedQuery(keyword="search-term")
        result = await query.validate()
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_query_authorize_returns_failure(self) -> None:
        query = ValidatedQuery()
        result = await query.authorize()
        assert result.authorized is False
        assert result.errors[0].resource == "search"
