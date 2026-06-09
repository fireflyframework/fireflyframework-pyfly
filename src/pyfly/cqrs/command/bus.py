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
"""CommandBus — central mediator for command processing.

Mirrors Java's ``CommandBus`` interface and ``DefaultCommandBus``
implementation.  The full pipeline is:

    correlate → validate → authorize → execute → metrics → events
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Protocol, TypeVar, runtime_checkable

from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContext
from pyfly.cqrs.exceptions import CommandProcessingException
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command
from pyfly.cqrs.validation.exceptions import CqrsValidationException

R = TypeVar("R")

_logger = logging.getLogger(__name__)


class EventFailureStrategy(enum.Enum):
    """Strategy for handling domain event publishing failures."""

    LOG = "log"
    """Log failures and continue (default). Command succeeds even if events fail."""

    RAISE = "raise"
    """Raise a CommandProcessingException if any event fails to publish."""


@runtime_checkable
class CommandBus(Protocol):
    """Port for sending commands through the CQRS pipeline."""

    async def send(self, command: Command[Any]) -> Any: ...

    async def send_with_context(self, command: Command[Any], context: ExecutionContext) -> Any: ...

    def register_handler(self, handler: CommandHandler[Any, Any]) -> None: ...

    def unregister_handler(self, command_type: type) -> None: ...

    def has_handler(self, command_type: type) -> bool: ...


class DefaultCommandBus:
    """Production-ready implementation of :class:`CommandBus`.

    Pipeline:
    1. Set correlation context
    2. Validate command (structural + custom)
    3. Authorize command
    4. Execute handler
    5. Publish domain events (if publisher available)
    6. Record metrics
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService | None = None,
        authorization: AuthorizationService | None = None,
        metrics: CqrsMetricsService | None = None,
        event_publisher: Any | None = None,
        event_failure_strategy: EventFailureStrategy = EventFailureStrategy.LOG,
    ) -> None:
        self._registry = registry
        self._validation = validation
        self._authorization = authorization
        self._metrics = metrics or CqrsMetricsService()
        self._event_publisher = event_publisher
        self._event_failure_strategy = event_failure_strategy

    # ── CommandBus protocol ────────────────────────────────────

    async def send(self, command: Command[Any]) -> Any:
        return await self._execute(command, context=None)

    async def send_with_context(self, command: Command[Any], context: ExecutionContext) -> Any:
        return await self._execute(command, context=context)

    def register_handler(self, handler: CommandHandler[Any, Any]) -> None:
        self._registry.register_command_handler(handler)

    def unregister_handler(self, command_type: type) -> None:
        self._registry.unregister_command_handler(command_type)

    def has_handler(self, command_type: type) -> bool:
        return self._registry.has_command_handler(command_type)

    # ── pipeline ───────────────────────────────────────────────

    async def _execute(self, command: Command[Any], context: ExecutionContext | None) -> Any:
        start = self._metrics.now()
        command_name = type(command).__name__
        previous_cid = CorrelationContext.get_correlation_id()

        try:
            # 1. Correlation
            cid = command.get_correlation_id() or CorrelationContext.get_or_create_correlation_id()
            CorrelationContext.set_correlation_id(cid)
            command.set_correlation_id(cid)

            # 2. Validate
            if self._validation:
                await self._validation.validate_command(command)

            # 3. Authorize
            if self._authorization:
                await self._authorization.authorize_command(command, context)

            # 4. Execute
            handler = self._registry.find_command_handler(type(command))
            if context is not None:
                result = await handler.handle_with_context(command, context)
            else:
                result = await handler.handle(command)

            # 5. Publish events
            if self._event_publisher:
                await self._try_publish_events(command, result)

            # 6. Metrics
            duration = self._metrics.now() - start
            self._metrics.record_command_success(command, duration)

            _logger.debug("Command %s processed in %.3fs", command_name, duration)
            return result

        except Exception as exc:
            duration = self._metrics.now() - start
            if isinstance(exc, CqrsValidationException):
                self._metrics.record_validation_failure(command)  # audit #99
            self._metrics.record_command_failure(command, exc, duration)
            if not isinstance(exc, CommandProcessingException):
                raise CommandProcessingException(
                    message=f"Failed to process command {command_name}: {exc}",
                    command_type=type(command),
                    cause=exc,
                ) from exc
            raise
        finally:
            # Restore the prior correlation id so it never leaks into the next
            # command on the same task/thread, while preserving an outer
            # (e.g. per-request) correlation id (audit #98).
            if previous_cid is None:
                CorrelationContext.clear()
            else:
                CorrelationContext.set_correlation_id(previous_cid)

    async def _try_publish_events(self, command: Any, result: Any) -> None:
        """Publish domain events if the handler/command produced any.

        The destination is resolved from the matched handler's
        ``__pyfly_event_destination__`` attribute (set by
        ``@publish_domain_event(destination=...)``).  When absent the
        publisher falls back to its own default.
        """
        publisher = self._event_publisher
        if publisher is None:
            return
        events = getattr(result, "domain_events", None) or getattr(command, "domain_events", None)
        if events:
            # Resolve the optional destination from the handler's decorator
            # metadata so the bus honours @publish_domain_event(destination=…).
            handler = self._registry.find_command_handler(type(command))
            destination: str | None = getattr(handler, "__pyfly_event_destination__", None)

            failed_events: list[tuple[Any, Exception]] = []
            for event in events:
                try:
                    await publisher.publish(event, destination=destination)
                except Exception as exc:
                    _logger.error("Failed to publish domain event %s: %s", type(event).__name__, exc)
                    failed_events.append((event, exc))
            if failed_events and self._event_failure_strategy == EventFailureStrategy.RAISE:
                first_event, first_exc = failed_events[0]
                raise CommandProcessingException(
                    message=(
                        f"{len(failed_events)} domain event(s) failed to publish "
                        f"for {type(command).__name__}; first failure: {first_exc}"
                    ),
                    command_type=type(command),
                    cause=first_exc,
                ) from first_exc
            elif failed_events:
                _logger.error("%d domain event(s) failed to publish", len(failed_events))
