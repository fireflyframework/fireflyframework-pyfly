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
"""The saga and TCC engines' persistence port, on the configured orchestration persistence provider.

The saga and TCC engines (and ``SagaRecoveryService``) persist through
:class:`~pyfly.transactional.shared.ports.outbound.TransactionalPersistencePort`, which stores plain
state dictionaries; the workflow engine and the recovery scan use
:class:`~pyfly.transactional.core.persistence.ExecutionPersistenceProvider`, which stores
:class:`~pyfly.transactional.core.persistence.ExecutionState` rows. :class:`ProviderPersistencePort`
implements the port on a provider, so ``pyfly.transactional.persistence.provider`` reaches every engine:
with ``sqlalchemy`` or ``redis``, a saga cut short by a crash is still there for the next process to
recover, and ``/api/orchestration/executions`` lists sagas and TCC transactions beside workflows.

A port state maps to an execution like this:

===============================  ===========================================================
Port state                       ``ExecutionState``
===============================  ===========================================================
``correlation_id``               ``correlation_id``
``saga_name`` / ``tcc_name``     ``name``, with pattern ``SAGA`` / ``TCC``
``status``                       ``IN_FLIGHT`` is ``RUNNING``; ``COMPLETED`` and ``FAILED`` stay
``started_at``                   ``started_at`` (a ``datetime`` or an ISO-8601 string)
(every write)                    ``updated_at``: what the stale scan compares
``completed_at``                 ``completed_at``
the whole state                  ``payload`` (JSON-safe: instants as ISO-8601 strings)
===============================  ===========================================================

Only saga and TCC executions are the port's: a workflow execution of the same provider is never returned
or cleaned up through it.

The engines write an execution's start (``persist_state``) and its end (``mark_completed``); they do not
record step statuses, which ``update_step_status`` offers to callers that track step progress.
``update_step_status`` and ``mark_completed`` read the execution, change it and save it. They are serialized
per execution (two updates of one execution that run together, such as the statuses of parallel steps, must
each keep the other's change), never across executions: a lock shared by every execution, held across the
database I/O, deadlocked with the connection pool when completions inside business transactions (each
holding its unit's connection) waited for it while its holder, a completion outside any transaction, waited
for a connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import ExecutionPersistenceProvider, ExecutionState

_PATTERNS = (ExecutionPattern.SAGA, ExecutionPattern.TCC)

_TO_STATUS = {
    "IN_FLIGHT": ExecutionStatus.RUNNING,
    "COMPLETED": ExecutionStatus.COMPLETED,
    "FAILED": ExecutionStatus.FAILED,
}


def _port_status(status: ExecutionStatus) -> str:
    if status is ExecutionStatus.COMPLETED:
        return "COMPLETED"
    return "FAILED" if status.is_terminal else "IN_FLIGHT"


def _instant(value: Any) -> datetime | None:
    """An aware instant from a ``datetime`` (naive is UTC) or an ISO-8601 string; ``None`` otherwise."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


class _PatternScopedCleanup(Protocol):
    async def cleanup(self, older_than: timedelta, *, patterns: Collection[ExecutionPattern] | None = None) -> int: ...


def _takes_keyword(function: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (parameter.KEYWORD_ONLY, parameter.POSITIONAL_OR_KEYWORD)


class _Serial:
    """The lock of one execution, and how many callers hold or wait for it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class ProviderPersistencePort:
    """A :class:`~pyfly.transactional.shared.ports.outbound.TransactionalPersistencePort` on an
    :class:`~pyfly.transactional.core.persistence.ExecutionPersistenceProvider` (see the module
    documentation).

    ``update_step_status`` and ``mark_completed`` read the execution, change it and save it; they are
    serialized per execution within the process (one engine drives a given execution), and executions never
    wait for one another.
    """

    def __init__(self, provider: ExecutionPersistenceProvider) -> None:
        self._provider = provider
        self._serials: dict[str, _Serial] = {}
        self._cleans_up_by_pattern = _takes_keyword(provider.cleanup, "patterns")

    @property
    def provider(self) -> ExecutionPersistenceProvider:
        """The provider the states are stored in."""
        return self._provider

    # -- persist / retrieve ------------------------------------------------------------------------------

    async def persist_state(self, state: dict[str, Any]) -> None:
        """Store the state of an execution that starts (``IN_FLIGHT`` unless *state* says otherwise)."""
        document = dict(state)
        document.setdefault("status", "IN_FLIGHT")
        now = datetime.now(UTC)
        document["started_at"] = _instant(document.get("started_at")) or now
        await self._provider.save(self._execution(document, updated_at=now))

    async def get_state(self, correlation_id: str) -> dict[str, Any] | None:
        """The state of saga or TCC execution *correlation_id*, or ``None``."""
        execution = await self._provider.find(correlation_id)
        if execution is None or execution.pattern not in _PATTERNS:
            return None
        return self._state(execution)

    # -- updates ------------------------------------------------------------------------------------------

    async def update_step_status(self, correlation_id: str, step_id: str, status: str) -> None:
        """Record *status* for step *step_id*. Raises ``KeyError`` for an unknown execution."""
        async with self._serialized(correlation_id):
            state = await self._require(correlation_id)
            steps: dict[str, dict[str, Any]] = state.setdefault("steps", {})
            steps.setdefault(step_id, {})["status"] = status
            await self._provider.save(self._execution(state, updated_at=datetime.now(UTC)))

    async def mark_completed(self, correlation_id: str, successful: bool) -> None:
        """Mark the execution ``COMPLETED`` or ``FAILED``. Raises ``KeyError`` for an unknown execution."""
        async with self._serialized(correlation_id):
            state = await self._require(correlation_id)
            now = datetime.now(UTC)
            state["status"] = "COMPLETED" if successful else "FAILED"
            state["successful"] = successful
            state["completed_at"] = now
            await self._provider.save(self._execution(state, updated_at=now))

    # -- queries ------------------------------------------------------------------------------------------

    async def get_in_flight(self) -> list[dict[str, Any]]:
        """Saga and TCC executions that have not completed."""
        running = await self._provider.find_all(status=ExecutionStatus.RUNNING)
        return [self._state(execution) for execution in running if execution.pattern in _PATTERNS]

    async def get_stale(self, before: datetime) -> list[dict[str, Any]]:
        """Saga and TCC executions not completed and last updated before *before*."""
        stale = await self._provider.find_stale(before)
        return [self._state(execution) for execution in stale if execution.pattern in _PATTERNS]

    async def cleanup(self, older_than: timedelta) -> int:
        """Delete the saga and TCC executions that completed more than *older_than* ago.

        A provider whose ``cleanup`` takes a ``patterns`` keyword (the SQL provider: one ``DELETE``) deletes
        them itself; with any other, they are listed and deleted one by one."""
        if self._cleans_up_by_pattern:
            scoped = cast("_PatternScopedCleanup", self._provider)
            return await scoped.cleanup(older_than, patterns=_PATTERNS)
        cutoff = datetime.now(UTC) - older_than
        deleted = 0
        for pattern in _PATTERNS:
            for execution in await self._provider.find_all(pattern=pattern):
                ended = execution.completed_at or execution.updated_at
                if (
                    execution.status.is_terminal
                    and ended < cutoff
                    and await self._provider.delete(execution.correlation_id)
                ):
                    deleted += 1
        return deleted

    async def is_healthy(self) -> bool:
        """Whether the provider's store answers."""
        return await self._provider.is_healthy()

    # -- mapping ------------------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _serialized(self, correlation_id: str) -> AsyncIterator[None]:
        """Hold execution *correlation_id*'s lock; the lock is dropped when its last user leaves."""
        serial = self._serials.get(correlation_id)
        if serial is None:
            serial = self._serials[correlation_id] = _Serial()
        serial.users += 1
        try:
            async with serial.lock:
                yield
        finally:
            serial.users -= 1
            if serial.users == 0 and self._serials.get(correlation_id) is serial:
                del self._serials[correlation_id]

    async def _require(self, correlation_id: str) -> dict[str, Any]:
        state = await self.get_state(correlation_id)
        if state is None:
            raise KeyError(correlation_id)
        return state

    @staticmethod
    def _execution(state: dict[str, Any], *, updated_at: datetime) -> ExecutionState:
        pattern = ExecutionPattern.TCC if "tcc_name" in state else ExecutionPattern.SAGA
        name = state.get("tcc_name") if pattern is ExecutionPattern.TCC else state.get("saga_name")
        started_at = _instant(state.get("started_at")) or updated_at
        return ExecutionState(
            correlation_id=str(state["correlation_id"]),
            name=str(name or "unknown"),
            pattern=pattern,
            status=_TO_STATUS.get(str(state.get("status", "IN_FLIGHT")), ExecutionStatus.RUNNING),
            started_at=started_at,
            updated_at=updated_at,
            completed_at=_instant(state.get("completed_at")),
            payload=_json_safe(state),
        )

    @staticmethod
    def _state(execution: ExecutionState) -> dict[str, Any]:
        state = dict(execution.payload)
        state["correlation_id"] = execution.correlation_id
        state["status"] = _port_status(execution.status)
        state["started_at"] = execution.started_at
        state["completed_at"] = execution.completed_at
        return state
