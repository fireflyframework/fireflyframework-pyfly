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
"""CQRS validation result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ValidationSeverity(StrEnum):
    """Severity level for a validation error."""

    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ValidationError:
    """A single validation failure."""

    field_name: str
    message: str
    error_code: str = "VALIDATION_ERROR"
    severity: ValidationSeverity = ValidationSeverity.ERROR
    rejected_value: object = None


@dataclass(frozen=True)
class ValidationResult:
    """Immutable result of a validation operation.

    Compose multiple results with :meth:`combine`.
    """

    valid: bool
    errors: tuple[ValidationError, ...] = ()
    summary: str | None = None

    # ── factories ──────────────────────────────────────────────

    @staticmethod
    def success() -> ValidationResult:
        return ValidationResult(valid=True)

    @staticmethod
    def failure(field_name: str, message: str, *, error_code: str = "VALIDATION_ERROR") -> ValidationResult:
        return ValidationResult(
            valid=False,
            errors=(ValidationError(field_name=field_name, message=message, error_code=error_code),),
        )

    @staticmethod
    def from_errors(errors: list[ValidationError]) -> ValidationResult:
        if not errors:
            return ValidationResult.success()
        return ValidationResult(valid=False, errors=tuple(errors))

    # ── combinators ────────────────────────────────────────────

    def combine(self, other: ValidationResult) -> ValidationResult:
        """Merge two results; invalid if either is invalid."""
        return ValidationResult(
            valid=self.valid and other.valid,
            errors=self.errors + other.errors,
        )

    # ── helpers ────────────────────────────────────────────────

    def error_messages(self) -> list[str]:
        return [f"{e.field_name}: {e.message}" for e in self.errors]
