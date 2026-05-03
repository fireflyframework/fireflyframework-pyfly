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
"""Command validation service — separates validation from the bus pipeline.

Mirrors Java's ``CommandValidationService``: runs structural (pydantic)
validation then custom business-rule validation.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.cqrs.validation.processor import AutoValidationProcessor
from pyfly.cqrs.validation.types import ValidationResult

_logger = logging.getLogger(__name__)


class CommandValidationService:
    """Validates commands (and queries) using the :class:`AutoValidationProcessor`."""

    def __init__(self, processor: AutoValidationProcessor | None = None) -> None:
        self._processor = processor or AutoValidationProcessor()

    async def validate_command(self, command: Any) -> None:
        """Validate and raise :class:`CqrsValidationException` on failure."""
        result = await self.validate_command_with_result(command)
        if not result.valid:
            _logger.warning(
                "Validation failed for %s: %s",
                type(command).__name__,
                result.error_messages(),
            )
            raise CqrsValidationException(result)

    async def validate_command_with_result(self, command: Any) -> ValidationResult:
        """Validate and return the result without raising."""
        return await self._processor.validate(command)

    async def validate_query(self, query: Any) -> None:
        """Validate a query; same pipeline as commands."""
        result = await self._processor.validate(query)
        if not result.valid:
            _logger.warning(
                "Validation failed for %s: %s",
                type(query).__name__,
                result.error_messages(),
            )
            raise CqrsValidationException(result)
