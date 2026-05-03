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
"""Command and query handler registry with auto-discovery.

Mirrors Java's ``CommandHandlerRegistry`` — scans beans from the DI
container that carry ``__pyfly_handler_type__`` markers set by the
``@command_handler`` / ``@query_handler`` decorators.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.exceptions import CommandHandlerNotFoundException, QueryHandlerNotFoundException
from pyfly.cqrs.query.handler import QueryHandler

_logger = logging.getLogger(__name__)


class HandlerRegistry:
    """Thread-safe registry of command and query handlers.

    Handlers are keyed by the message type they handle (discovered via
    ``get_command_type()`` / ``get_query_type()``).
    """

    def __init__(self) -> None:
        self._command_handlers: dict[type, CommandHandler[Any, Any]] = {}
        self._query_handlers: dict[type, QueryHandler[Any, Any]] = {}

    # ── registration ───────────────────────────────────────────

    def register_command_handler(self, handler: CommandHandler[Any, Any]) -> None:
        cmd_type = handler.get_command_type()
        if cmd_type is None:
            _logger.warning("Cannot register %s: command type unresolvable", type(handler).__name__)
            return
        if cmd_type in self._command_handlers:
            _logger.warning(
                "Replacing command handler for %s: %s -> %s",
                cmd_type.__name__,
                type(self._command_handlers[cmd_type]).__name__,
                type(handler).__name__,
            )
        self._command_handlers[cmd_type] = handler
        _logger.debug("Registered command handler %s for %s", type(handler).__name__, cmd_type.__name__)

    def register_query_handler(self, handler: QueryHandler[Any, Any]) -> None:
        query_type = handler.get_query_type()
        if query_type is None:
            _logger.warning("Cannot register %s: query type unresolvable", type(handler).__name__)
            return
        if query_type in self._query_handlers:
            _logger.warning(
                "Replacing query handler for %s: %s -> %s",
                query_type.__name__,
                type(self._query_handlers[query_type]).__name__,
                type(handler).__name__,
            )
        self._query_handlers[query_type] = handler
        _logger.debug("Registered query handler %s for %s", type(handler).__name__, query_type.__name__)

    # ── unregistration ─────────────────────────────────────────

    def unregister_command_handler(self, command_type: type) -> bool:
        return self._command_handlers.pop(command_type, None) is not None

    def unregister_query_handler(self, query_type: type) -> bool:
        return self._query_handlers.pop(query_type, None) is not None

    # ── lookup ─────────────────────────────────────────────────

    def find_command_handler(self, command_type: type) -> CommandHandler[Any, Any]:
        handler = self._command_handlers.get(command_type)
        if handler is None:
            raise CommandHandlerNotFoundException(command_type)
        return handler

    def find_query_handler(self, query_type: type) -> QueryHandler[Any, Any]:
        handler = self._query_handlers.get(query_type)
        if handler is None:
            raise QueryHandlerNotFoundException(query_type)
        return handler

    def has_command_handler(self, command_type: type) -> bool:
        return command_type in self._command_handlers

    def has_query_handler(self, query_type: type) -> bool:
        return query_type in self._query_handlers

    # ── introspection ──────────────────────────────────────────

    @property
    def command_handler_count(self) -> int:
        return len(self._command_handlers)

    @property
    def query_handler_count(self) -> int:
        return len(self._query_handlers)

    def get_registered_command_types(self) -> set[type]:
        return set(self._command_handlers.keys())

    def get_registered_query_types(self) -> set[type]:
        return set(self._query_handlers.keys())

    # ── auto-discovery ─────────────────────────────────────────

    def discover_from_beans(self, beans: list[Any]) -> None:
        """Scan a list of bean instances for ``@command_handler`` / ``@query_handler`` markers."""
        for bean in beans:
            handler_type = getattr(type(bean), "__pyfly_handler_type__", None)
            if handler_type == "command" and isinstance(bean, CommandHandler):
                self.register_command_handler(bean)
            elif handler_type == "query" and isinstance(bean, QueryHandler):
                self.register_query_handler(bean)
