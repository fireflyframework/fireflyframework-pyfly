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
"""Outbound port protocols for the shared transactional engine.

These ``@runtime_checkable`` ``Protocol`` definitions form the hexagonal
architecture boundary between the transactional engine and its infrastructure
adapters.  Adapters (persistence, observability, backpressure, error handling)
must satisfy the structural contracts defined here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pyfly.transactional.shared.types import BackpressureConfig


@runtime_checkable
class TransactionalPersistencePort(Protocol):
    """Port for persisting and querying transactional execution state.

    Adapters provide durable storage (relational DB, NoSQL, in-memory) so that
    the engine can survive restarts, detect in-flight transactions, and drive
    recovery / cleanup.
    """

    async def persist_state(self, state: dict[str, Any]) -> None:
        """Persist the initial (or full) state of a transactional context."""
        ...

    async def get_state(self, correlation_id: str) -> dict[str, Any] | None:
        """Return the persisted state for *correlation_id*, or ``None`` if absent."""
        ...

    async def update_step_status(self, correlation_id: str, step_id: str, status: str) -> None:
        """Update the status of a single step within the identified transaction."""
        ...

    async def mark_completed(self, correlation_id: str, successful: bool) -> None:
        """Mark the transaction identified by *correlation_id* as completed.

        *successful* distinguishes a clean commit from a compensated/rolled-back
        outcome.
        """
        ...

    async def get_in_flight(self) -> list[dict[str, Any]]:
        """Return all transactions that have been started but not yet completed."""
        ...

    async def get_stale(self, before: datetime) -> list[dict[str, Any]]:
        """Return transactions whose last update timestamp is older than *before*."""
        ...

    async def cleanup(self, older_than: timedelta) -> int:
        """Delete completed transaction records older than *older_than*.

        Returns the number of records deleted.
        """
        ...

    async def is_healthy(self) -> bool:
        """Return ``True`` if the underlying storage backend is reachable."""
        ...


@runtime_checkable
class TransactionalEventsPort(Protocol):
    """Port for emitting lifecycle events from the transactional engine.

    Adapters integrate with observability back-ends (metrics, tracing, audit
    logs) without coupling the engine to any specific vendor.
    """

    async def on_start(self, name: str, correlation_id: str) -> None:
        """Fired when a named transaction begins execution."""
        ...

    async def on_step_success(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        attempts: int,
        latency_ms: float,
    ) -> None:
        """Fired when an individual step completes successfully."""
        ...

    async def on_step_failed(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception,
        attempts: int,
        latency_ms: float,
    ) -> None:
        """Fired when an individual step fails (after all retries are exhausted)."""
        ...

    async def on_compensated(
        self,
        name: str,
        correlation_id: str,
        step_id: str,
        error: Exception | None,
    ) -> None:
        """Fired after a compensating action has been executed for a step.

        *error* is ``None`` when the compensation itself succeeded.
        """
        ...

    async def on_completed(self, name: str, correlation_id: str, success: bool) -> None:
        """Fired when the entire transaction finishes (committed or compensated)."""
        ...


@runtime_checkable
class BackpressureStrategyPort(Protocol):
    """Port for pluggable backpressure / concurrency-control strategies.

    Implementations control how a batch of *items* is fed through *processor*
    according to the given *config* (rate limits, concurrency caps, batch
    sizes, etc.).
    """

    async def apply(
        self,
        items: list[Any],
        processor: Callable[[Any], Awaitable[Any]],
        config: BackpressureConfig,
    ) -> list[Any]:
        """Process *items* via *processor* under the configured backpressure policy.

        Returns the collected results in the same order as *items*.
        """
        ...

    @property
    def strategy_name(self) -> str:
        """Human-readable identifier for this strategy (e.g. ``"token_bucket"``)."""
        ...


@runtime_checkable
class CompensationErrorHandlerPort(Protocol):
    """Port for handling errors that occur *during* compensation.

    When a compensating action itself fails, the engine delegates to this port
    rather than raising an unhandled exception, allowing adapters to dead-letter,
    alert, or apply domain-specific recovery logic.
    """

    async def handle(self, saga_name: str, step_id: str, error: Exception, ctx: Any) -> None:
        """Handle a compensation failure for the given step.

        Args:
            saga_name: Name of the saga / TCC context that triggered compensation.
            step_id:   Identifier of the step whose compensating action failed.
            error:     The exception raised by the compensating action.
            ctx:       Execution context (saga state, correlation metadata, etc.).
        """
        ...
