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
"""Centralized authorization orchestration for CQRS messages.

Mirrors Java's ``AuthorizationService`` — calls the message's own
``authorize()`` / ``authorize_with_context()`` hooks and raises
:class:`AuthorizationException` on denial.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.cqrs.authorization.exceptions import AuthorizationException
from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.context.execution_context import ExecutionContext

_logger = logging.getLogger(__name__)


class AuthorizationService:
    """Evaluates authorization for commands and queries.

    When ``enabled=False`` every request is automatically authorized.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # ── commands ───────────────────────────────────────────────

    async def authorize_command(self, command: Any, context: ExecutionContext | None = None) -> None:
        """Authorize a command; raises :class:`AuthorizationException` on denial."""
        if not self._enabled:
            return
        result = await self._evaluate(command, context)
        if not result.authorized:
            _logger.warning(
                "Authorization denied for command %s: %s",
                type(command).__name__,
                result.error_messages(),
            )
            raise AuthorizationException(result)

    # ── queries ────────────────────────────────────────────────

    async def authorize_query(self, query: Any, context: ExecutionContext | None = None) -> None:
        """Authorize a query; raises :class:`AuthorizationException` on denial."""
        if not self._enabled:
            return
        result = await self._evaluate(query, context)
        if not result.authorized:
            _logger.warning(
                "Authorization denied for query %s: %s",
                type(query).__name__,
                result.error_messages(),
            )
            raise AuthorizationException(result)

    # ── internals ──────────────────────────────────────────────

    @staticmethod
    async def _evaluate(message: Any, context: ExecutionContext | None) -> AuthorizationResult:
        """Call the message's authorize hooks."""
        if context is not None:
            authorize_ctx = getattr(message, "authorize_with_context", None)
            if authorize_ctx is not None:
                result = authorize_ctx(context)
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, AuthorizationResult):
                    return result

        authorize_fn = getattr(message, "authorize", None)
        if authorize_fn is not None:
            result = authorize_fn()
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, AuthorizationResult):
                return result

        return AuthorizationResult.success()
