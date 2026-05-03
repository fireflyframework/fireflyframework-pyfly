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
"""Fluent builder for creating and executing commands.

Mirrors Java's ``CommandBuilder`` — reduces boilerplate when constructing
commands and dispatching them through the bus.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pyfly.cqrs.types import Command

C = TypeVar("C", bound=Command)  # type: ignore[type-arg]
R = TypeVar("R")


class CommandBuilder(Generic[C, R]):
    """Fluent builder for command creation and optional execution.

    Usage::

        result = await (
            CommandBuilder.create(CreateOrderCommand)
            .with_field("customer_id", "cust-1")
            .with_field("items", [item1, item2])
            .correlated_by("req-abc")
            .initiated_by("user-42")
            .execute_with(command_bus)
        )
    """

    def __init__(self, command_type: type[C]) -> None:
        self._command_type = command_type
        self._fields: dict[str, Any] = {}
        self._correlation_id: str | None = None
        self._initiated_by: str | None = None
        self._timestamp: datetime | None = None
        self._metadata: dict[str, Any] = {}

    @staticmethod
    def create(command_type: type[C]) -> CommandBuilder[C, R]:
        return CommandBuilder(command_type)

    # ── domain fields ──────────────────────────────────────────

    def with_field(self, name: str, value: Any) -> CommandBuilder[C, R]:
        self._fields[name] = value
        return self

    def with_fields(self, **kwargs: Any) -> CommandBuilder[C, R]:
        self._fields.update(kwargs)
        return self

    # ── metadata ───────────────────────────────────────────────

    def correlated_by(self, correlation_id: str) -> CommandBuilder[C, R]:
        self._correlation_id = correlation_id
        return self

    def initiated_by(self, user_id: str) -> CommandBuilder[C, R]:
        self._initiated_by = user_id
        return self

    def at(self, timestamp: datetime) -> CommandBuilder[C, R]:
        self._timestamp = timestamp
        return self

    def with_metadata(self, key: str, value: Any) -> CommandBuilder[C, R]:
        self._metadata[key] = value
        return self

    # ── build ──────────────────────────────────────────────────

    def build(self) -> C:
        """Construct the command instance.

        Domain-specific fields are passed as constructor kwargs.
        Metadata is set via the Command method API afterward, which
        safely works even on frozen dataclass subclasses.
        """
        command = self._command_type(**self._fields)
        if self._correlation_id:
            command.set_correlation_id(self._correlation_id)
        if self._initiated_by:
            command.set_initiated_by(self._initiated_by)
        if self._timestamp is not None:
            object.__setattr__(command, "_cqrs_timestamp", self._timestamp)
        for k, v in self._metadata.items():
            command.set_metadata(k, v)
        return command

    # ── build + execute ────────────────────────────────────────

    async def execute_with(self, bus: Any) -> Any:
        """Build the command and dispatch it through a :class:`CommandBus`."""
        command = self.build()
        return await bus.send(command)
