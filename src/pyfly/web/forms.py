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
"""Form limits and safe validation feedback."""

from dataclasses import dataclass
from typing import Any

from pyfly.core.config import config_properties
from pyfly.kernel.exceptions import ValidationException


@config_properties(prefix="pyfly.web.forms")
@dataclass
class FormProperties:
    max_fields: int = 1000
    max_files: int = 20
    max_part_size: int = 1024 * 1024
    max_body_size: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_fields, self.max_files, self.max_part_size, self.max_body_size) < 1:
            raise ValueError("Form limits must be positive")


class FormValidationException(ValidationException):
    def __init__(self, errors: list[dict[str, Any]], values: dict[str, Any]) -> None:
        self.errors = errors
        self.values = values
        super().__init__("Invalid form", code="FORM_VALIDATION", context={"errors": errors})
