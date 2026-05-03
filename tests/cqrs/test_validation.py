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
"""Tests for CQRS validation types and AutoValidationProcessor."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cqrs.types import Command
from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.cqrs.validation.processor import AutoValidationProcessor
from pyfly.cqrs.validation.types import ValidationError, ValidationResult, ValidationSeverity

# ── ValidationSeverity enum tests ─────────────────────────────


class TestValidationSeverity:
    def test_warning_value(self) -> None:
        assert ValidationSeverity.WARNING.value == "WARNING"

    def test_error_value(self) -> None:
        assert ValidationSeverity.ERROR.value == "ERROR"

    def test_critical_value(self) -> None:
        assert ValidationSeverity.CRITICAL.value == "CRITICAL"

    def test_is_str_enum(self) -> None:
        assert isinstance(ValidationSeverity.ERROR, str)
        assert ValidationSeverity.ERROR == "ERROR"

    def test_all_members_present(self) -> None:
        members = {m.name for m in ValidationSeverity}
        assert members == {"WARNING", "ERROR", "CRITICAL"}


# ── ValidationError tests ────────────────────────────────────


class TestValidationError:
    def test_required_fields(self) -> None:
        error = ValidationError(field_name="email", message="invalid format")
        assert error.field_name == "email"
        assert error.message == "invalid format"

    def test_default_error_code(self) -> None:
        error = ValidationError(field_name="name", message="required")
        assert error.error_code == "VALIDATION_ERROR"

    def test_custom_error_code(self) -> None:
        error = ValidationError(field_name="age", message="too young", error_code="MIN_AGE")
        assert error.error_code == "MIN_AGE"

    def test_default_severity(self) -> None:
        error = ValidationError(field_name="name", message="required")
        assert error.severity == ValidationSeverity.ERROR

    def test_custom_severity(self) -> None:
        error = ValidationError(
            field_name="notes",
            message="too long",
            severity=ValidationSeverity.WARNING,
        )
        assert error.severity == ValidationSeverity.WARNING

    def test_rejected_value_default_none(self) -> None:
        error = ValidationError(field_name="name", message="required")
        assert error.rejected_value is None

    def test_rejected_value_can_be_set(self) -> None:
        error = ValidationError(
            field_name="age",
            message="must be positive",
            rejected_value=-5,
        )
        assert error.rejected_value == -5

    def test_frozen_immutability(self) -> None:
        error = ValidationError(field_name="name", message="required")
        with pytest.raises(AttributeError):
            error.field_name = "other"  # type: ignore[misc]


# ── ValidationResult tests ───────────────────────────────────


class TestValidationResult:
    def test_success_is_valid(self) -> None:
        result = ValidationResult.success()
        assert result.valid is True
        assert result.errors == ()
        assert result.summary is None

    def test_failure_is_invalid(self) -> None:
        result = ValidationResult.failure("email", "invalid email")
        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].field_name == "email"
        assert result.errors[0].message == "invalid email"

    def test_failure_with_custom_error_code(self) -> None:
        result = ValidationResult.failure("email", "bad format", error_code="INVALID_EMAIL")
        assert result.errors[0].error_code == "INVALID_EMAIL"

    def test_failure_default_error_code(self) -> None:
        result = ValidationResult.failure("name", "required")
        assert result.errors[0].error_code == "VALIDATION_ERROR"

    def test_from_errors_empty_list_returns_success(self) -> None:
        result = ValidationResult.from_errors([])
        assert result.valid is True
        assert result.errors == ()

    def test_from_errors_with_errors(self) -> None:
        errors = [
            ValidationError(field_name="name", message="required"),
            ValidationError(field_name="email", message="invalid"),
        ]
        result = ValidationResult.from_errors(errors)
        assert result.valid is False
        assert len(result.errors) == 2

    def test_combine_both_valid(self) -> None:
        r1 = ValidationResult.success()
        r2 = ValidationResult.success()
        combined = r1.combine(r2)
        assert combined.valid is True
        assert combined.errors == ()

    def test_combine_first_invalid(self) -> None:
        r1 = ValidationResult.failure("name", "required")
        r2 = ValidationResult.success()
        combined = r1.combine(r2)
        assert combined.valid is False
        assert len(combined.errors) == 1

    def test_combine_second_invalid(self) -> None:
        r1 = ValidationResult.success()
        r2 = ValidationResult.failure("email", "invalid")
        combined = r1.combine(r2)
        assert combined.valid is False
        assert len(combined.errors) == 1

    def test_combine_both_invalid_merges_errors(self) -> None:
        r1 = ValidationResult.failure("name", "required")
        r2 = ValidationResult.failure("email", "invalid")
        combined = r1.combine(r2)
        assert combined.valid is False
        assert len(combined.errors) == 2
        field_names = {e.field_name for e in combined.errors}
        assert field_names == {"name", "email"}

    def test_error_messages(self) -> None:
        result = ValidationResult.failure("email", "invalid format")
        messages = result.error_messages()
        assert messages == ["email: invalid format"]

    def test_error_messages_multiple(self) -> None:
        r1 = ValidationResult.failure("name", "required")
        r2 = ValidationResult.failure("age", "must be positive")
        combined = r1.combine(r2)
        messages = combined.error_messages()
        assert len(messages) == 2
        assert "name: required" in messages
        assert "age: must be positive" in messages

    def test_error_messages_empty_on_success(self) -> None:
        result = ValidationResult.success()
        assert result.error_messages() == []

    def test_frozen_immutability(self) -> None:
        result = ValidationResult.success()
        with pytest.raises(AttributeError):
            result.valid = False  # type: ignore[misc]


# ── CqrsValidationException tests ────────────────────────────


class TestCqrsValidationException:
    def test_contains_result(self) -> None:
        result = ValidationResult.failure("name", "required")
        exc = CqrsValidationException(result)
        assert exc.result is result
        assert exc.result.valid is False

    def test_message_from_error_messages(self) -> None:
        result = ValidationResult.failure("email", "invalid format")
        exc = CqrsValidationException(result)
        assert "email: invalid format" in str(exc)

    def test_custom_message(self) -> None:
        result = ValidationResult.failure("name", "required")
        exc = CqrsValidationException(result, message="Custom validation message")
        assert "Custom validation message" in str(exc)

    def test_is_exception(self) -> None:
        result = ValidationResult.failure("name", "required")
        exc = CqrsValidationException(result)
        assert isinstance(exc, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        result = ValidationResult.failure("name", "required")
        with pytest.raises(CqrsValidationException) as exc_info:
            raise CqrsValidationException(result)
        assert exc_info.value.result.valid is False


# ── AutoValidationProcessor tests ─────────────────────────────


@dataclass(frozen=True)
class ValidCommand(Command[None]):
    name: str = "valid"

    async def validate(self) -> ValidationResult:
        return ValidationResult.success()


@dataclass(frozen=True)
class InvalidCommand(Command[None]):
    name: str = ""

    async def validate(self) -> ValidationResult:
        if not self.name:
            return ValidationResult.failure("name", "name is required")
        return ValidationResult.success()


class PlainObject:
    """Object without a validate() method."""

    pass


class SyncValidateObject:
    """Object with a synchronous validate() that returns ValidationResult."""

    def validate(self) -> ValidationResult:
        return ValidationResult.failure("field", "sync fail")


class TestAutoValidationProcessor:
    @pytest.mark.asyncio
    async def test_validate_calls_custom_validate_success(self) -> None:
        processor = AutoValidationProcessor()
        cmd = ValidCommand(name="ok")
        result = await processor.validate(cmd)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_validate_calls_custom_validate_failure(self) -> None:
        processor = AutoValidationProcessor()
        cmd = InvalidCommand(name="")
        result = await processor.validate(cmd)
        assert result.valid is False
        assert len(result.errors) >= 1
        field_names = {e.field_name for e in result.errors}
        assert "name" in field_names

    @pytest.mark.asyncio
    async def test_validate_with_no_validate_method_returns_success(self) -> None:
        processor = AutoValidationProcessor()
        obj = PlainObject()
        result = await processor.validate(obj)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_validate_with_sync_validate_method(self) -> None:
        processor = AutoValidationProcessor()
        obj = SyncValidateObject()
        result = await processor.validate(obj)
        assert result.valid is False
        assert result.errors[0].field_name == "field"

    def test_validate_sync_on_plain_object_returns_success(self) -> None:
        processor = AutoValidationProcessor()
        obj = PlainObject()
        result = processor.validate_sync(obj)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_validate_command_with_valid_name(self) -> None:
        processor = AutoValidationProcessor()
        cmd = InvalidCommand(name="ok-name")
        result = await processor.validate(cmd)
        assert result.valid is True
