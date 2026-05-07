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
"""Unified ExecutionContext — shared state across every orchestration pattern.

Mirrors ``org.fireflyframework.orchestration.core.context.ExecutionContext``
in the Java engine.  All access is made via instance methods so adapters can
swap the backing store later (currently dict-based, asyncio.Lock-protected).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pyfly.transactional.core.model import (
    ExecutionPattern,
    ExecutionStatus,
    StepStatus,
    TccPhase,
)


@dataclass
class StepRecord:
    """Per-step bookkeeping inside an :class:`ExecutionContext`."""

    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any = None
    error: str | None = None
    compensation_result: Any = None
    compensation_error: str | None = None
    latency_ms: float = 0.0


class ExecutionContext:
    """Shared execution state for a single saga / workflow / TCC run.

    The context is process-local but persistable — every mutator is async to
    keep the callsite uniform (some persistence adapters need to round-trip
    state on each update).

    Args:
        name: Logical name of the saga/workflow/TCC.
        pattern: Which orchestration pattern this execution belongs to.
        correlation_id: Unique id for the run (auto-generated if omitted).
        input: Original payload passed to the engine entry point.
        headers: Free-form metadata (HTTP headers, message envelope keys…).
        dry_run: When ``True``, persistence side-effects are skipped.
    """

    def __init__(
        self,
        *,
        name: str,
        pattern: ExecutionPattern,
        correlation_id: str | None = None,
        input: Any = None,
        headers: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.name = name
        self.pattern = pattern
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.input = input
        self.headers: dict[str, str] = dict(headers or {})
        self.dry_run = dry_run

        self.status: ExecutionStatus = ExecutionStatus.PENDING
        self.tcc_phase: TccPhase | None = None
        self.started_at: datetime = datetime.now(UTC)
        self.updated_at: datetime = self.started_at
        self.completed_at: datetime | None = None
        self.error: str | None = None

        # Per-step bookkeeping.
        self._steps: dict[str, StepRecord] = {}
        # Mutable user variables (set/read by step methods via @SetVariable / @Variable).
        self._variables: dict[str, Any] = {}
        # Idempotency: set of keys already executed.
        self._idempotency_keys: set[str] = set()
        # Try-phase results (TCC).
        self._try_results: dict[str, Any] = {}
        # Currently waiting signals / timers (workflow).
        self._waiting_signals: dict[str, asyncio.Future[Any]] = {}
        self._delivered_signals: dict[str, Any] = {}

        self._lock = asyncio.Lock()

    # -- Variables ----------------------------------------------------------

    async def set_variable(self, key: str, value: Any) -> None:
        async with self._lock:
            self._variables[key] = value
            self._touch()

    def get_variable(self, key: str, default: Any = None) -> Any:
        return self._variables.get(key, default)

    def get_all_variables(self) -> dict[str, Any]:
        return dict(self._variables)

    # -- Step results -------------------------------------------------------

    async def record_step_started(self, step_id: str) -> None:
        async with self._lock:
            rec = self._steps.setdefault(step_id, StepRecord())
            rec.status = StepStatus.RUNNING
            rec.attempts += 1
            rec.started_at = datetime.now(UTC)
            self._touch()

    async def record_step_success(self, step_id: str, result: Any, latency_ms: float) -> None:
        async with self._lock:
            rec = self._steps.setdefault(step_id, StepRecord())
            rec.status = StepStatus.DONE
            rec.result = result
            rec.completed_at = datetime.now(UTC)
            rec.latency_ms = latency_ms
            self._touch()

    async def record_step_failure(self, step_id: str, error: BaseException, latency_ms: float) -> None:
        async with self._lock:
            rec = self._steps.setdefault(step_id, StepRecord())
            rec.status = StepStatus.FAILED
            rec.error = str(error)
            rec.completed_at = datetime.now(UTC)
            rec.latency_ms = latency_ms
            self._touch()

    async def record_step_skipped(self, step_id: str) -> None:
        async with self._lock:
            rec = self._steps.setdefault(step_id, StepRecord())
            rec.status = StepStatus.SKIPPED
            self._touch()

    async def record_step_compensated(self, step_id: str, result: Any, error: BaseException | None) -> None:
        async with self._lock:
            rec = self._steps.setdefault(step_id, StepRecord())
            rec.compensation_result = result
            rec.compensation_error = str(error) if error else None
            rec.status = (
                StepStatus.COMPENSATION_FAILED if error else StepStatus.COMPENSATED
            )
            self._touch()

    def get_step(self, step_id: str) -> StepRecord | None:
        return self._steps.get(step_id)

    def get_step_result(self, step_id: str) -> Any:
        rec = self._steps.get(step_id)
        return rec.result if rec else None

    def get_all_steps(self) -> dict[str, StepRecord]:
        return dict(self._steps)

    def is_step_done(self, step_id: str) -> bool:
        rec = self._steps.get(step_id)
        return rec is not None and rec.status in {
            StepStatus.DONE,
            StepStatus.SKIPPED,
        }

    # -- Idempotency --------------------------------------------------------

    async def remember_idempotency(self, key: str) -> bool:
        """Atomically reserve *key*; return ``True`` if newly added."""
        async with self._lock:
            if key in self._idempotency_keys:
                return False
            self._idempotency_keys.add(key)
            return True

    def has_idempotency(self, key: str) -> bool:
        return key in self._idempotency_keys

    # -- TCC ----------------------------------------------------------------

    async def set_tcc_phase(self, phase: TccPhase) -> None:
        async with self._lock:
            self.tcc_phase = phase
            self._touch()

    async def record_try_result(self, participant_id: str, result: Any) -> None:
        async with self._lock:
            self._try_results[participant_id] = result
            self._touch()

    def get_try_result(self, participant_id: str) -> Any:
        return self._try_results.get(participant_id)

    def get_all_try_results(self) -> dict[str, Any]:
        return dict(self._try_results)

    # -- Workflow signals ---------------------------------------------------

    async def wait_for_signal(self, name: str, timeout_ms: int = 0) -> Any:
        """Suspend until *name* is delivered, or timeout."""
        async with self._lock:
            if name in self._delivered_signals:
                return self._delivered_signals.pop(name)
            future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
            self._waiting_signals[name] = future
        try:
            if timeout_ms > 0:
                return await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return await future
        finally:
            async with self._lock:
                self._waiting_signals.pop(name, None)

    async def deliver_signal(self, name: str, payload: Any = None) -> bool:
        """Deliver *payload* to a waiter or buffer it.  Returns ``True`` if a waiter consumed it."""
        async with self._lock:
            future = self._waiting_signals.pop(name, None)
            if future is not None and not future.done():
                future.set_result(payload)
                return True
            self._delivered_signals[name] = payload
            return False

    # -- Status -------------------------------------------------------------

    async def set_status(self, status: ExecutionStatus, error: BaseException | None = None) -> None:
        async with self._lock:
            self.status = status
            if error is not None:
                self.error = str(error)
            if status.is_terminal:
                self.completed_at = datetime.now(UTC)
            self._touch()

    def _touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    # -- Header helpers -----------------------------------------------------

    def get_header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    # -- Dict round-trip ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern.value,
            "correlation_id": self.correlation_id,
            "status": self.status.value,
            "tcc_phase": self.tcc_phase.value if self.tcc_phase else None,
            "input": self.input,
            "headers": dict(self.headers),
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "variables": dict(self._variables),
            "steps": {
                sid: {
                    "status": rec.status.value,
                    "attempts": rec.attempts,
                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                    "completed_at": rec.completed_at.isoformat() if rec.completed_at else None,
                    "result": rec.result,
                    "error": rec.error,
                    "compensation_result": rec.compensation_result,
                    "compensation_error": rec.compensation_error,
                    "latency_ms": rec.latency_ms,
                }
                for sid, rec in self._steps.items()
            },
            "idempotency_keys": list(self._idempotency_keys),
            "try_results": dict(self._try_results),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionContext:
        ctx = cls(
            name=data["name"],
            pattern=ExecutionPattern(data["pattern"]),
            correlation_id=data["correlation_id"],
            input=data.get("input"),
            headers=data.get("headers", {}),
        )
        ctx.status = ExecutionStatus(data["status"])
        if data.get("tcc_phase"):
            ctx.tcc_phase = TccPhase(data["tcc_phase"])
        ctx.started_at = datetime.fromisoformat(data["started_at"])
        ctx.updated_at = datetime.fromisoformat(data["updated_at"])
        ctx.completed_at = (
            datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None
        )
        ctx.error = data.get("error")
        ctx._variables = dict(data.get("variables", {}))
        for sid, raw in data.get("steps", {}).items():
            rec = StepRecord(
                status=StepStatus(raw["status"]),
                attempts=raw.get("attempts", 0),
                started_at=datetime.fromisoformat(raw["started_at"]) if raw.get("started_at") else None,
                completed_at=datetime.fromisoformat(raw["completed_at"]) if raw.get("completed_at") else None,
                result=raw.get("result"),
                error=raw.get("error"),
                compensation_result=raw.get("compensation_result"),
                compensation_error=raw.get("compensation_error"),
                latency_ms=raw.get("latency_ms", 0.0),
            )
            ctx._steps[sid] = rec
        ctx._idempotency_keys = set(data.get("idempotency_keys", []))
        ctx._try_results = dict(data.get("try_results", {}))
        return ctx
