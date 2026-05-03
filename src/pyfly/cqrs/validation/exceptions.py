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
"""CQRS validation exceptions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.cqrs.exceptions import CqrsException

if TYPE_CHECKING:
    from pyfly.cqrs.validation.types import ValidationResult


class CqrsValidationException(CqrsException):
    """Raised when command/query validation fails."""

    def __init__(self, result: ValidationResult, message: str | None = None) -> None:
        self.result = result
        summary = message or "; ".join(result.error_messages()) or "Validation failed"
        super().__init__(
            message=summary,
            code="VALIDATION_FAILED",
            context={"errors": [{"field": e.field_name, "message": e.message} for e in result.errors]},
        )
