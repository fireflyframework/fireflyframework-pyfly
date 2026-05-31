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
"""In-memory implementation of :class:`TransactionalPersistencePort`.

This adapter stores all transactional state in a plain Python ``dict``,
making it the zero-dependency default when no external database is
configured.  **All state is lost on process restart.**
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any


class InMemoryPersistenceAdapter:
    """In-memory :class:`TransactionalPersistencePort`.  State lost on restart.

    Each entry in the internal ``_store`` is keyed by *correlation_id* and
    holds the full transaction state dict::

        {
            "correlation_id": "abc-123",
            "status": "IN_FLIGHT",          # IN_FLIGHT | COMPLETED | FAILED
            "started_at": datetime,
            "completed_at": datetime | None,
            "successful": bool | None,
            "steps": {
                "step-id": {"status": "DONE"}
            },
        }
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # -- persist / retrieve -------------------------------------------------

    async def persist_state(self, state: dict[str, Any]) -> None:
        """Persist the initial (or full) state of a transactional context.

        *state* **must** contain a ``"correlation_id"`` key.  If ``"status"``
        or ``"started_at"`` are absent they default to ``"IN_FLIGHT"`` and
        ``datetime.now(UTC)`` respectively.
        """
        correlation_id: str = state["correlation_id"]
        state.setdefault("status", "IN_FLIGHT")
        state.setdefault("started_at", datetime.now(UTC))
        async with self._lock:
            self._store[correlation_id] = state

    async def get_state(self, correlation_id: str) -> dict[str, Any] | None:
        """Return the persisted state for *correlation_id*, or ``None``."""
        async with self._lock:
            return self._store.get(correlation_id)

    # -- step-level updates -------------------------------------------------

    async def update_step_status(self, correlation_id: str, step_id: str, status: str) -> None:
        """Update the status of *step_id* within the given transaction.

        Initialises the ``"steps"`` dict and/or the individual step entry if
        they do not yet exist.
        """
        async with self._lock:
            state = self._store[correlation_id]
            steps: dict[str, dict[str, Any]] = state.setdefault("steps", {})
            step = steps.setdefault(step_id, {})
            step["status"] = status

    # -- completion ---------------------------------------------------------

    async def mark_completed(self, correlation_id: str, successful: bool) -> None:
        """Mark the transaction as ``COMPLETED`` or ``FAILED``."""
        async with self._lock:
            state = self._store[correlation_id]
            state["status"] = "COMPLETED" if successful else "FAILED"
            state["successful"] = successful
            state["completed_at"] = datetime.now(UTC)

    # -- queries ------------------------------------------------------------

    async def get_in_flight(self) -> list[dict[str, Any]]:
        """Return all transactions whose status is ``IN_FLIGHT``."""
        async with self._lock:
            return [s for s in self._store.values() if s.get("status") == "IN_FLIGHT"]

    async def get_stale(self, before: datetime) -> list[dict[str, Any]]:
        """Return transactions whose ``started_at`` is older than *before*.

        Tolerates ``started_at`` stored as either a ``datetime`` or an ISO-8601
        string so a comparison never raises ``TypeError`` regardless of how the
        caller persisted it.
        """
        async with self._lock:
            stale: list[dict[str, Any]] = []
            for s in self._store.values():
                started = s.get("started_at")
                if started is None:
                    continue
                if isinstance(started, str):
                    try:
                        started = datetime.fromisoformat(started)
                    except ValueError:
                        continue
                if started < before:
                    stale.append(s)
            return stale

    # -- maintenance --------------------------------------------------------

    async def cleanup(self, older_than: timedelta) -> int:
        """Remove completed/failed transactions older than *older_than*.

        Returns the number of records deleted.
        """
        cutoff = datetime.now(UTC) - older_than
        async with self._lock:
            to_remove: list[str] = []
            for cid, state in self._store.items():
                if state.get("status") in ("COMPLETED", "FAILED"):
                    completed_at = state.get("completed_at")
                    if completed_at is not None and completed_at < cutoff:
                        to_remove.append(cid)

            for cid in to_remove:
                del self._store[cid]

        return len(to_remove)

    async def is_healthy(self) -> bool:
        """In-memory store is always healthy."""
        return True
