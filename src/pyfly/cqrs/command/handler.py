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
"""Enhanced command handler with lifecycle hooks.

Mirrors Java's ``CommandHandler`` abstract class which defines a
template-method pipeline::

    pre_process  ->  do_handle  ->  post_process  ->  on_success
                                                       on_error (if exception)
"""

from __future__ import annotations

import logging
from types import get_original_bases as get_orig_bases
from typing import Generic, TypeVar, get_args

from pyfly.cqrs.context.execution_context import ExecutionContext

C = TypeVar("C")  # Command type
R = TypeVar("R")  # Result type

_logger = logging.getLogger(__name__)


class CommandHandler(Generic[C, R]):
    """Base class for command handlers with lifecycle hooks.

    Subclasses **must** implement :meth:`do_handle`.  All other hooks
    are optional and default to no-ops.

    Example::

        @command_handler
        @service
        class CreateOrderHandler(CommandHandler[CreateOrderCommand, OrderId]):
            def __init__(self, repo: OrderRepository) -> None:
                self._repo = repo

            async def do_handle(self, command: CreateOrderCommand) -> OrderId:
                order = Order.create(command.customer_id, command.items)
                return await self._repo.save(order)
    """

    def __init__(self) -> None:
        self._command_type: type | None = self._resolve_command_type()

    def _resolve_command_type(self) -> type | None:
        """Introspect Generic[C, R] to discover the concrete command class."""
        for base in get_orig_bases(type(self)):
            origin = getattr(base, "__origin__", None)
            if origin is not None and (
                origin is CommandHandler or (isinstance(origin, type) and issubclass(origin, CommandHandler))
            ):
                args = get_args(base)
                if args:
                    return args[0] if isinstance(args[0], type) else None
        return None

    def get_command_type(self) -> type | None:
        return self._command_type

    # ── template method (final) ────────────────────────────────

    async def handle(self, command: C) -> R:
        """Execute the full handler pipeline.  Do not override."""
        try:
            await self.pre_process(command)
            result = await self.do_handle(command)
            await self.post_process(command, result)
            await self.on_success(command, result)
            return result
        except Exception as exc:
            await self.on_error(command, exc)
            raise self.map_error(command, exc) from exc

    async def handle_with_context(self, command: C, context: ExecutionContext) -> R:
        """Execute with an :class:`ExecutionContext`.

        Override :meth:`do_handle_with_context` for context-aware logic.
        Falls back to :meth:`handle` by default.
        """
        try:
            await self.pre_process(command)
            result = await self.do_handle_with_context(command, context)
            await self.post_process(command, result)
            await self.on_success(command, result)
            return result
        except Exception as exc:
            await self.on_error(command, exc)
            raise self.map_error(command, exc) from exc

    # ── abstract ───────────────────────────────────────────────

    async def do_handle(self, command: C) -> R:
        """Business logic — **must** be overridden by subclass."""
        raise NotImplementedError

    async def do_handle_with_context(self, command: C, context: ExecutionContext) -> R:
        """Context-aware business logic — defaults to :meth:`do_handle`."""
        return await self.do_handle(command)

    # ── lifecycle hooks ────────────────────────────────────────

    async def pre_process(self, command: C) -> None:
        """Called before ``do_handle``.  Override for setup / enrichment."""

    async def post_process(self, command: C, result: R) -> None:
        """Called after ``do_handle`` on success.  Override for side-effects."""

    async def on_success(self, command: C, result: R) -> None:
        """Called after ``post_process``.  Override for metrics / logging."""

    async def on_error(self, command: C, error: Exception) -> None:
        """Called when ``do_handle`` raises.  Override for error reporting."""
        _logger.error(
            "Command %s failed: %s",
            type(command).__name__,
            error,
            exc_info=True,
        )

    def map_error(self, command: C, error: Exception) -> Exception:
        """Transform an exception before it propagates.  Override for mapping."""
        return error


class ContextAwareCommandHandler(CommandHandler[C, R]):
    """Base class for handlers that **require** an :class:`ExecutionContext`.

    Overrides :meth:`do_handle` to raise so callers must use
    :meth:`handle_with_context`.
    """

    async def do_handle(self, command: C) -> R:
        raise RuntimeError(
            f"{type(self).__name__} requires an ExecutionContext. Use handle_with_context() instead of handle()."
        )

    async def do_handle_with_context(self, command: C, context: ExecutionContext) -> R:
        raise NotImplementedError
