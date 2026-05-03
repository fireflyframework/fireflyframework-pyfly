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
"""CQRS exception hierarchy."""

from __future__ import annotations

from typing import Any

from pyfly.kernel.exceptions import BusinessException, InfrastructureException


class CqrsException(BusinessException):
    """Base exception for all CQRS errors."""

    def __init__(self, message: str, code: str | None = None, context: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code=code or "CQRS_ERROR", context=context)


class CommandHandlerNotFoundException(CqrsException):
    """No handler registered for the given command type."""

    def __init__(self, command_type: type) -> None:
        self.command_type = command_type
        super().__init__(
            message=f"No handler registered for command: {command_type.__name__}",
            code="COMMAND_HANDLER_NOT_FOUND",
            context={"command_type": command_type.__name__},
        )


class QueryHandlerNotFoundException(CqrsException):
    """No handler registered for the given query type."""

    def __init__(self, query_type: type) -> None:
        self.query_type = query_type
        super().__init__(
            message=f"No handler registered for query: {query_type.__name__}",
            code="QUERY_HANDLER_NOT_FOUND",
            context={"query_type": query_type.__name__},
        )


class CommandProcessingException(CqrsException):
    """Error during command processing pipeline."""

    def __init__(
        self,
        message: str,
        command_type: type | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.command_type = command_type
        self.cause = cause
        ctx: dict[str, Any] = {}
        if command_type:
            ctx["command_type"] = command_type.__name__
        if cause:
            ctx["cause"] = str(cause)
        super().__init__(message=message, code="COMMAND_PROCESSING_ERROR", context=ctx)


class QueryProcessingException(CqrsException):
    """Error during query processing pipeline."""

    def __init__(
        self,
        message: str,
        query_type: type | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.query_type = query_type
        self.cause = cause
        ctx: dict[str, Any] = {}
        if query_type:
            ctx["query_type"] = query_type.__name__
        if cause:
            ctx["cause"] = str(cause)
        super().__init__(message=message, code="QUERY_PROCESSING_ERROR", context=ctx)


class CqrsConfigurationException(InfrastructureException):
    """CQRS auto-configuration or wiring error."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="CQRS_CONFIGURATION_ERROR")
