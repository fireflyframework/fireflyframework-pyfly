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
"""Tests for CQRS authorization types and AuthorizationService."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cqrs.authorization.exceptions import AuthorizationException
from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.authorization.types import (
    AuthorizationError,
    AuthorizationResult,
    AuthorizationSeverity,
)
from pyfly.cqrs.context.execution_context import DefaultExecutionContext
from pyfly.cqrs.types import Command, Query

# ── AuthorizationSeverity enum tests ──────────────────────────


class TestAuthorizationSeverity:
    def test_warning_value(self) -> None:
        assert AuthorizationSeverity.WARNING.value == "WARNING"

    def test_error_value(self) -> None:
        assert AuthorizationSeverity.ERROR.value == "ERROR"

    def test_critical_value(self) -> None:
        assert AuthorizationSeverity.CRITICAL.value == "CRITICAL"

    def test_is_str_enum(self) -> None:
        assert isinstance(AuthorizationSeverity.ERROR, str)
        assert AuthorizationSeverity.ERROR == "ERROR"

    def test_all_members_present(self) -> None:
        members = {m.name for m in AuthorizationSeverity}
        assert members == {"WARNING", "ERROR", "CRITICAL"}


# ── AuthorizationError tests ─────────────────────────────────


class TestAuthorizationError:
    def test_required_fields(self) -> None:
        error = AuthorizationError(resource="orders", message="access denied")
        assert error.resource == "orders"
        assert error.message == "access denied"

    def test_default_error_code(self) -> None:
        error = AuthorizationError(resource="orders", message="denied")
        assert error.error_code == "AUTHORIZATION_ERROR"

    def test_custom_error_code(self) -> None:
        error = AuthorizationError(
            resource="orders",
            message="denied",
            error_code="ROLE_MISMATCH",
        )
        assert error.error_code == "ROLE_MISMATCH"

    def test_default_severity(self) -> None:
        error = AuthorizationError(resource="orders", message="denied")
        assert error.severity == AuthorizationSeverity.ERROR

    def test_custom_severity(self) -> None:
        error = AuthorizationError(
            resource="audit",
            message="logged warning",
            severity=AuthorizationSeverity.WARNING,
        )
        assert error.severity == AuthorizationSeverity.WARNING

    def test_denied_action_default_none(self) -> None:
        error = AuthorizationError(resource="orders", message="denied")
        assert error.denied_action is None

    def test_denied_action_can_be_set(self) -> None:
        error = AuthorizationError(
            resource="orders",
            message="cannot delete",
            denied_action="DELETE",
        )
        assert error.denied_action == "DELETE"

    def test_frozen_immutability(self) -> None:
        error = AuthorizationError(resource="orders", message="denied")
        with pytest.raises(AttributeError):
            error.resource = "other"  # type: ignore[misc]


# ── AuthorizationResult tests ────────────────────────────────


class TestAuthorizationResult:
    def test_success_is_authorized(self) -> None:
        result = AuthorizationResult.success()
        assert result.authorized is True
        assert result.errors == ()
        assert result.summary is None

    def test_failure_is_not_authorized(self) -> None:
        result = AuthorizationResult.failure("orders", "access denied")
        assert result.authorized is False
        assert len(result.errors) == 1
        assert result.errors[0].resource == "orders"
        assert result.errors[0].message == "access denied"

    def test_failure_with_custom_error_code(self) -> None:
        result = AuthorizationResult.failure(
            "orders",
            "denied",
            error_code="INSUFFICIENT_ROLE",
        )
        assert result.errors[0].error_code == "INSUFFICIENT_ROLE"

    def test_failure_with_denied_action(self) -> None:
        result = AuthorizationResult.failure(
            "orders",
            "cannot delete",
            denied_action="DELETE",
        )
        assert result.errors[0].denied_action == "DELETE"

    def test_failure_default_error_code(self) -> None:
        result = AuthorizationResult.failure("orders", "denied")
        assert result.errors[0].error_code == "AUTHORIZATION_ERROR"

    def test_combine_both_authorized(self) -> None:
        r1 = AuthorizationResult.success()
        r2 = AuthorizationResult.success()
        combined = r1.combine(r2)
        assert combined.authorized is True
        assert combined.errors == ()

    def test_combine_first_unauthorized(self) -> None:
        r1 = AuthorizationResult.failure("orders", "denied")
        r2 = AuthorizationResult.success()
        combined = r1.combine(r2)
        assert combined.authorized is False
        assert len(combined.errors) == 1

    def test_combine_second_unauthorized(self) -> None:
        r1 = AuthorizationResult.success()
        r2 = AuthorizationResult.failure("users", "denied")
        combined = r1.combine(r2)
        assert combined.authorized is False
        assert len(combined.errors) == 1

    def test_combine_both_unauthorized_merges_errors(self) -> None:
        r1 = AuthorizationResult.failure("orders", "cannot read")
        r2 = AuthorizationResult.failure("users", "cannot write")
        combined = r1.combine(r2)
        assert combined.authorized is False
        assert len(combined.errors) == 2
        resources = {e.resource for e in combined.errors}
        assert resources == {"orders", "users"}

    def test_error_messages(self) -> None:
        result = AuthorizationResult.failure("orders", "access denied")
        messages = result.error_messages()
        assert messages == ["orders: access denied"]

    def test_error_messages_multiple(self) -> None:
        r1 = AuthorizationResult.failure("orders", "read denied")
        r2 = AuthorizationResult.failure("users", "write denied")
        combined = r1.combine(r2)
        messages = combined.error_messages()
        assert len(messages) == 2
        assert "orders: read denied" in messages
        assert "users: write denied" in messages

    def test_error_messages_empty_on_success(self) -> None:
        result = AuthorizationResult.success()
        assert result.error_messages() == []

    def test_frozen_immutability(self) -> None:
        result = AuthorizationResult.success()
        with pytest.raises(AttributeError):
            result.authorized = False  # type: ignore[misc]


# ── AuthorizationException tests ──────────────────────────────


class TestAuthorizationException:
    def test_contains_result(self) -> None:
        result = AuthorizationResult.failure("orders", "denied")
        exc = AuthorizationException(result)
        assert exc.result is result
        assert exc.result.authorized is False

    def test_message_from_error_messages(self) -> None:
        result = AuthorizationResult.failure("orders", "access denied")
        exc = AuthorizationException(result)
        assert "orders: access denied" in str(exc)

    def test_custom_message(self) -> None:
        result = AuthorizationResult.failure("orders", "denied")
        exc = AuthorizationException(result, message="Custom auth message")
        assert "Custom auth message" in str(exc)

    def test_is_exception(self) -> None:
        result = AuthorizationResult.failure("orders", "denied")
        exc = AuthorizationException(result)
        assert isinstance(exc, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        result = AuthorizationResult.failure("orders", "denied")
        with pytest.raises(AuthorizationException) as exc_info:
            raise AuthorizationException(result)
        assert exc_info.value.result.authorized is False


# ── test command / query types for AuthorizationService ───────


@dataclass(frozen=True)
class AllowedCommand(Command[str]):
    name: str = "allowed"

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.success()


@dataclass(frozen=True)
class DeniedCommand(Command[None]):
    name: str = "denied"

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.failure(
            resource="orders",
            message="not authorized to create orders",
            denied_action="CREATE",
        )


@dataclass(frozen=True)
class ContextDeniedCommand(Command[None]):
    name: str = "ctx-denied"

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.success()

    async def authorize_with_context(self, ctx: object) -> AuthorizationResult:
        return AuthorizationResult.failure(
            resource="orders",
            message="context says no",
        )


@dataclass(frozen=True)
class AllowedQuery(Query[dict]):
    query_id_val: str = "q-1"

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.success()


@dataclass(frozen=True)
class DeniedQuery(Query[None]):
    query_id_val: str = "q-2"

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.failure(
            resource="reports",
            message="not authorized to view reports",
        )


# ── AuthorizationService tests ───────────────────────────────


class TestAuthorizationService:
    @pytest.mark.asyncio
    async def test_enabled_allows_authorized_command(self) -> None:
        service = AuthorizationService(enabled=True)
        cmd = AllowedCommand()
        await service.authorize_command(cmd)

    @pytest.mark.asyncio
    async def test_enabled_denies_unauthorized_command(self) -> None:
        service = AuthorizationService(enabled=True)
        cmd = DeniedCommand()

        with pytest.raises(AuthorizationException) as exc_info:
            await service.authorize_command(cmd)

        assert exc_info.value.result.authorized is False
        assert exc_info.value.result.errors[0].resource == "orders"
        assert exc_info.value.result.errors[0].denied_action == "CREATE"

    @pytest.mark.asyncio
    async def test_disabled_skips_authorization_for_command(self) -> None:
        service = AuthorizationService(enabled=False)
        cmd = DeniedCommand()
        await service.authorize_command(cmd)

    @pytest.mark.asyncio
    async def test_enabled_allows_authorized_query(self) -> None:
        service = AuthorizationService(enabled=True)
        query = AllowedQuery()
        await service.authorize_query(query)

    @pytest.mark.asyncio
    async def test_enabled_denies_unauthorized_query(self) -> None:
        service = AuthorizationService(enabled=True)
        query = DeniedQuery()

        with pytest.raises(AuthorizationException) as exc_info:
            await service.authorize_query(query)

        assert exc_info.value.result.authorized is False
        assert exc_info.value.result.errors[0].resource == "reports"

    @pytest.mark.asyncio
    async def test_disabled_skips_authorization_for_query(self) -> None:
        service = AuthorizationService(enabled=False)
        query = DeniedQuery()
        await service.authorize_query(query)

    def test_is_enabled_property_true(self) -> None:
        service = AuthorizationService(enabled=True)
        assert service.is_enabled is True

    def test_is_enabled_property_false(self) -> None:
        service = AuthorizationService(enabled=False)
        assert service.is_enabled is False

    def test_default_enabled_is_true(self) -> None:
        service = AuthorizationService()
        assert service.is_enabled is True

    @pytest.mark.asyncio
    async def test_authorize_command_with_context(self) -> None:
        service = AuthorizationService(enabled=True)
        cmd = ContextDeniedCommand()
        ctx = DefaultExecutionContext(user_id="user-1")

        with pytest.raises(AuthorizationException) as exc_info:
            await service.authorize_command(cmd, context=ctx)

        assert exc_info.value.result.authorized is False
        assert "context says no" in exc_info.value.result.errors[0].message

    @pytest.mark.asyncio
    async def test_authorize_command_without_context_uses_authorize(self) -> None:
        service = AuthorizationService(enabled=True)
        cmd = ContextDeniedCommand()
        # Without context, falls through to authorize() which returns success
        await service.authorize_command(cmd)

    @pytest.mark.asyncio
    async def test_authorize_object_without_authorize_method_succeeds(self) -> None:
        service = AuthorizationService(enabled=True)

        class PlainMessage:
            pass

        await service.authorize_command(PlainMessage())

    @pytest.mark.asyncio
    async def test_authorize_query_with_context(self) -> None:
        service = AuthorizationService(enabled=True)

        @dataclass(frozen=True)
        class CtxDeniedQuery(Query[None]):
            async def authorize_with_context(self, ctx: object) -> AuthorizationResult:
                return AuthorizationResult.failure("data", "context denied")

        query = CtxDeniedQuery()
        ctx = DefaultExecutionContext(user_id="user-1")

        with pytest.raises(AuthorizationException):
            await service.authorize_query(query, context=ctx)

    @pytest.mark.asyncio
    async def test_exception_contains_error_details(self) -> None:
        service = AuthorizationService(enabled=True)
        cmd = DeniedCommand()

        with pytest.raises(AuthorizationException) as exc_info:
            await service.authorize_command(cmd)

        exc = exc_info.value
        assert exc.result.errors[0].message == "not authorized to create orders"
        assert "orders" in str(exc)
