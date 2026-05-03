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
"""Automatic validation processor for commands and queries.

Uses pydantic model validation when available, falling back to the
object's own ``validate()`` method.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.cqrs.validation.types import ValidationError, ValidationResult, ValidationSeverity

_logger = logging.getLogger(__name__)


class AutoValidationProcessor:
    """Validates commands and queries using pydantic and custom rules.

    Pipeline:
    1. Pydantic model validation (if the object is a pydantic ``BaseModel``)
    2. Custom validation via ``obj.validate()`` (if the method exists)
    3. Combine all results
    """

    def validate_sync(self, obj: Any) -> ValidationResult:
        """Synchronous structural validation (pydantic fields)."""
        return self._validate_pydantic(obj)

    async def validate(self, obj: Any) -> ValidationResult:
        """Full async validation: structural + custom business rules."""
        structural = self._validate_pydantic(obj)
        custom = await self._validate_custom(obj)
        return structural.combine(custom)

    # ── internals ──────────────────────────────────────────────

    @staticmethod
    def _validate_pydantic(obj: Any) -> ValidationResult:
        """Validate using pydantic if the object is a BaseModel."""
        try:
            from pydantic import BaseModel
            from pydantic import ValidationError as PydanticError

            if isinstance(obj, BaseModel):
                try:
                    obj.model_validate(obj.model_dump())
                    return ValidationResult.success()
                except PydanticError as exc:
                    errors = [
                        ValidationError(
                            field_name=".".join(str(loc) for loc in e["loc"]),
                            message=e["msg"],
                            error_code=e["type"],
                            severity=ValidationSeverity.ERROR,
                            rejected_value=e.get("input"),
                        )
                        for e in exc.errors()
                    ]
                    return ValidationResult.from_errors(errors)
        except ImportError:
            pass
        return ValidationResult.success()

    @staticmethod
    async def _validate_custom(obj: Any) -> ValidationResult:
        """Call obj.validate() if present."""
        validate_fn = getattr(obj, "validate", None)
        if validate_fn is None:
            return ValidationResult.success()
        try:
            result = validate_fn()
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, ValidationResult):
                return result
            return ValidationResult.success()
        except Exception as exc:
            _logger.warning("Custom validation raised: %s", exc)
            return ValidationResult.failure("_custom", str(exc))
