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
"""Error enums and RFC 7807-inspired ErrorResponse model.

This module provides structured error types for consistent error reporting
across the framework. All types use only the Python standard library.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCategory(Enum):
    """Classifies an error by its origin or domain."""

    VALIDATION = "VALIDATION"
    BUSINESS = "BUSINESS"
    TECHNICAL = "TECHNICAL"
    SECURITY = "SECURITY"
    EXTERNAL = "EXTERNAL"
    RESOURCE = "RESOURCE"
    RATE_LIMIT = "RATE_LIMIT"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class ErrorSeverity(Enum):
    """Indicates the severity level of an error."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class FieldError:
    """Describes a validation error on a single field."""

    field: str
    message: str
    rejected_value: Any = None


@dataclass
class ErrorResponse:
    """RFC 7807-inspired structured error response.

    Core fields are always present. Optional fields are excluded from
    ``to_dict()`` output when they are ``None`` or empty.
    """

    # Core (always present)
    timestamp: str
    status: int
    error: str
    message: str
    code: str
    path: str

    # Tracing (optional)
    trace_id: str | None = None
    span_id: str | None = None
    transaction_id: str | None = None

    # Classification
    category: ErrorCategory = ErrorCategory.TECHNICAL
    severity: ErrorSeverity = ErrorSeverity.MEDIUM

    # Resilience
    retryable: bool = False
    retry_after: int | None = None

    # Validation
    field_errors: list[FieldError] = field(default_factory=list)

    # Debug
    debug_info: dict[str, Any] | None = None
    suggestion: str | None = None
    documentation_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON responses.

        Core fields, category, severity, and retryable are always included.
        All other optional fields are excluded when ``None`` or empty.
        """
        # Always-present fields
        result: dict[str, Any] = {
            "timestamp": self.timestamp,
            "status": self.status,
            "error": self.error,
            "message": self.message,
            "code": self.code,
            "path": self.path,
            "category": self.category.value,
            "severity": self.severity.value,
            "retryable": self.retryable,
        }

        # Optional scalar fields — include only when not None
        _optional_scalars = (
            "trace_id",
            "span_id",
            "transaction_id",
            "retry_after",
            "suggestion",
            "documentation_url",
        )
        for attr in _optional_scalars:
            value = getattr(self, attr)
            if value is not None:
                result[attr] = value

        # Field errors — include only when non-empty
        if self.field_errors:
            result["field_errors"] = [dataclasses.asdict(fe) for fe in self.field_errors]

        # Debug info — include only when not None
        if self.debug_info is not None:
            result["debug_info"] = self.debug_info

        return result
