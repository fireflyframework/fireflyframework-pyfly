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

    correlate → validate → authorize → execute → query-cache invalidation → events → metrics

Query-cache invalidation (with a ``query_cache``): once the command's unit of work commits (at once when
it runs outside one; nothing when it fails or rolls back), the bus evicts

- the command's ``get_cache_key()`` (a query's cache key) for every caller, also under every registered
  query handler's ``cache_key_prefix``;
- every cached result of the query handlers tagged with ``@cache_evict(EventType)`` for an event the
  command produced (``domain_events`` on its result or on the command) or for an event its own handler is
  tagged with.

When the handler ends with a cancellation or a commit whose outcome is unknown
(:class:`~pyfly.data.transaction.CommitOutcomeUnknownError`), its own unit of work may have committed (a
commit completes, shielded, before the cancellation is delivered): the bus evicts what it would have after a
commit, then re-raises. An eviction too many costs a cache miss; one too few keeps a stale result.

Event publication: the command's ``domain_events`` are published once its unit of work commits, through an
``after_commit`` synchronization (at once when it runs outside one; not at all when it rolls back), so a
wider unit that fails after the command publishes nothing. A publisher that joins transactions (an outbox
bus: ``joins_transactions``) publishes at once instead, inside the unit, which commits or rolls back the
events with it. Deferred to the commit, a publication failure is logged and counted by the unit, not raised
(``EventFailureStrategy.RAISE`` applies when the bus publishes at once).
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any, Protocol, TypeVar, runtime_checkable

from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.cache.adapter import QueryCacheAdapter, evict_query_key, query_cache_group
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContext
from pyfly.cqrs.exceptions import CommandProcessingException
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command, Query
from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.data.transaction import CommitOutcomeUnknownError, after_commit
from pyfly.data.transaction.template import shielded

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
    5. Invalidate the query cache after the commit (if a query cache is available)
    6. Publish domain events (if publisher available)
    7. Record metrics

    The invalidation comes before the publication: a handler whose own ``@transactional`` has committed
    must not leave its old results cached because an event then fails to publish
    (``EventFailureStrategy.RAISE``). Inside a wider unit of work the invalidation waits for its commit, so
    a publication failure that rolls that unit back drops it.
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        validation: CommandValidationService | None = None,
        authorization: AuthorizationService | None = None,
        metrics: CqrsMetricsService | None = None,
        event_publisher: Any | None = None,
        event_failure_strategy: EventFailureStrategy = EventFailureStrategy.LOG,
        *,
        query_cache: QueryCacheAdapter | None = None,
    ) -> None:
        self._registry = registry
        self._validation = validation
        self._authorization = authorization
        self._metrics = metrics or CqrsMetricsService()
        self._event_publisher = event_publisher
        self._event_failure_strategy = event_failure_strategy
        self._query_cache = query_cache
        self._warned_groups: set[type] = set()

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
            try:
                if context is not None:
                    result = await handler.handle_with_context(command, context)
                else:
                    result = await handler.handle(command)
            except (asyncio.CancelledError, CommitOutcomeUnknownError):
                # The handler's own unit may have committed before the cancellation (or the lost connection)
                # reached it: evict what it made stale all the same, then re-raise.
                await self._invalidate_after_unknown_outcome(command, handler)
                raise

            # 5. Query-cache invalidation (after the commit inside a unit of work). It runs to completion even
            # when the task is cancelled meanwhile: the handler may have committed already.
            if self._query_cache is not None and self._query_cache.is_available:
                stale = self._stale_queries(command, result, handler)
                if stale is not None:
                    await shielded(self._invalidate_queries(command, *stale))

            # 6. Publish events
            if self._event_publisher:
                await self._try_publish_events(command, result)

            # 7. Metrics
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

    async def _invalidate_after_unknown_outcome(self, command: Command[Any], handler: CommandHandler[Any, Any]) -> None:
        if self._query_cache is None or not self._query_cache.is_available:
            return
        stale = self._stale_queries(command, None, handler)
        if stale is not None:
            await shielded(self._invalidate_queries(command, *stale))

    def _stale_queries(
        self, command: Command[Any], result: Any, handler: CommandHandler[Any, Any]
    ) -> tuple[str | None, list[QueryHandler[Any, Any]]] | None:
        """What *command* made stale (see the module docs): its query cache key, and the cacheable query
        handlers tagged with an event it produced; ``None`` when that is nothing (no eviction to run)."""
        try:
            key = command.get_cache_key() or None
            produced = self._produced_event_types(command, result, handler)
            tagged: list[QueryHandler[Any, Any]] = []
            if produced:
                for query_type in sorted(self._registry.get_registered_query_types(), key=lambda t: t.__qualname__):
                    query_handler = self._registry.find_query_handler(query_type)
                    tags = query_handler.get_cache_evict_events()
                    if query_handler.supports_caching() and any(issubclass(p, t) for p in produced for t in tags):
                        tagged.append(query_handler)
        except Exception as exc:  # noqa: BLE001 — the command has run; a stale cache must not fail it
            _logger.error("Query-cache invalidation failed for %s: %s", type(command).__name__, exc, exc_info=True)
            return None
        if key is None and not tagged:
            return None
        return key, tagged

    async def _invalidate_queries(
        self, command: Command[Any], key: str | None, tagged: list[QueryHandler[Any, Any]]
    ) -> None:
        """Evict *key* for every caller and the groups of the *tagged* query handlers. The query cache defers
        the evictions to the commit of the current unit of work; a failing eviction is logged, never raised."""
        cache = self._query_cache
        assert cache is not None
        try:
            if key is not None:
                await evict_query_key(cache, self._registry, key)
            for query_handler in tagged:
                self._warn_if_unreachable(query_handler)
                await cache.evict_prefix(query_cache_group(query_handler))
        except Exception as exc:  # noqa: BLE001 — the command has run; a stale cache must not fail it
            _logger.error("Query-cache invalidation failed for %s: %s", type(command).__name__, exc, exc_info=True)

    def _warn_if_unreachable(self, query_handler: Any) -> None:
        """Warn once when an event-tag eviction of *query_handler* may reach none of its entries: its query
        builds its own keys (``get_cache_key()`` overridden) and the handler declares no
        ``cache_key_prefix``, so its entries need not start with the ``<QueryClass>:`` group evicted."""
        query_type = query_handler.get_query_type()
        if (
            type(query_handler) in self._warned_groups
            or query_handler.get_cache_key_prefix()
            or query_type is None
            or getattr(query_type, "get_cache_key", None) is Query.get_cache_key
        ):
            return
        self._warned_groups.add(type(query_handler))
        _logger.warning(
            "query_cache_eviction_may_miss handler=%s: its query %s overrides get_cache_key(), so an event-tag "
            "eviction of the group %r may reach none of its entries; declare @cacheable(cache_key_prefix=...) "
            "on the handler",
            type(query_handler).__qualname__,
            query_type.__qualname__,
            query_cache_group(query_handler),
        )

    @staticmethod
    def _produced_event_types(command: Any, result: Any, handler: CommandHandler[Any, Any]) -> set[type]:
        events = getattr(result, "domain_events", None) or getattr(command, "domain_events", None) or ()
        produced = {type(event) for event in events}
        produced.update(t for t in getattr(type(handler), "__pyfly_cache_evict_events__", ()) if isinstance(t, type))
        return produced

    async def _try_publish_events(self, command: Any, result: Any) -> None:
        """Publish domain events if the handler/command produced any, after the commit (module documentation).

        The destination is resolved from the matched handler's
        ``__pyfly_event_destination__`` attribute (set by
        ``@publish_domain_event(destination=...)``).  When absent the
        publisher falls back to its own default.
        """
        publisher = self._event_publisher
        if publisher is None:
            return
        events = list(getattr(result, "domain_events", None) or getattr(command, "domain_events", None) or ())
        if not events:
            return
        # Resolve the optional destination from the handler's decorator
        # metadata so the bus honours @publish_domain_event(destination=…).
        handler = self._registry.find_command_handler(type(command))
        destination: str | None = getattr(handler, "__pyfly_event_destination__", None)
        if getattr(publisher, "joins_transactions", False):
            await self._publish_events(command, events, destination)  # in the unit, which carries them
            return

        async def publish_after_commit() -> None:
            await self._publish_events(command, events, destination)

        await after_commit(publish_after_commit)

    async def _publish_events(self, command: Any, events: list[Any], destination: str | None) -> None:
        publisher = self._event_publisher
        assert publisher is not None
        failed_events: list[tuple[Any, Exception]] = []
        for event in events:
            try:
                await publisher.publish(event, destination=destination)
            except Exception as exc:
                _logger.error("Failed to publish domain event %s: %s", type(event).__name__, exc, exc_info=True)
                failed_events.append((event, exc))
        if failed_events and self._event_failure_strategy == EventFailureStrategy.RAISE:
            _first_event, first_exc = failed_events[0]
            raise CommandProcessingException(
                message=(
                    f"{len(failed_events)} domain event(s) failed to publish "
                    f"for {type(command).__name__}; first failure: {first_exc}"
                ),
                command_type=type(command),
                cause=first_exc,
            ) from first_exc
        if failed_events:
            _logger.error("%d domain event(s) failed to publish", len(failed_events))
