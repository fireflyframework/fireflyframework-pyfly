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
"""Pydantic integration helpers for validation."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pyfly.kernel.exceptions import ValidationException

T = TypeVar("T", bound=BaseModel)


def validate_model(model: type[T], data: dict[str, Any]) -> T:
    """Validate data against a Pydantic model.

    Raises:
        ValidationException: If validation fails, with structured error details.
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        detail = "; ".join(f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in errors)
        raise ValidationException(
            f"Validation failed: {detail}",
            code="VALIDATION_ERROR",
            context={"errors": errors},
        ) from exc
