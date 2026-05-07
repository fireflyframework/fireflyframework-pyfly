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
"""Lifecycle observability events emitted by the orchestration engine.

Mirrors the Java ``OrchestrationEvents`` interface and its default
implementations (logger + composite).  Adapters can subscribe to forward
events into Micrometer, OpenTelemetry, custom audit logs, etc.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from pyfly.transactional.core.model import ExecutionPattern, TccPhase

_logger = logging.getLogger(__name__)


@runtime_checkable
class OrchestrationEvents(Protocol):
    """Hooks fired during saga / workflow / TCC execution.

    Every method has a default no-op implementation in concrete classes;
    subclasses override only the events they care about.
    """

    async def on_start(self, *, name: str, pattern: ExecutionPattern, correlation_id: str) -> None: ...
    async def on_completed(
        self, *, name: str, pattern: ExecutionPattern, correlation_id: str, success: bool, duration_ms: float
    ) -> None: ...
    async def on_step_started(self, *, name: str, correlation_id: str, step_id: str) -> None: ...
    async def on_step_success(
        self, *, name: str, correlation_id: str, step_id: str, attempts: int, latency_ms: float
    ) -> None: ...
    async def on_step_failed(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException, attempts: int, latency_ms: float
    ) -> None: ...
    async def on_step_skipped(self, *, name: str, correlation_id: str, step_id: str) -> None: ...
    async def on_compensation_started(self, *, name: str, correlation_id: str) -> None: ...
    async def on_step_compensated(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException | None
    ) -> None: ...
    async def on_phase_started(self, *, name: str, correlation_id: str, phase: TccPhase) -> None: ...
    async def on_phase_completed(
        self, *, name: str, correlation_id: str, phase: TccPhase, duration_ms: float
    ) -> None: ...
    async def on_phase_failed(
        self, *, name: str, correlation_id: str, phase: TccPhase, error: BaseException
    ) -> None: ...
    async def on_participant_started(
        self, *, name: str, correlation_id: str, phase: TccPhase, participant_id: str
    ) -> None: ...
    async def on_participant_success(
        self, *, name: str, correlation_id: str, phase: TccPhase, participant_id: str
    ) -> None: ...
    async def on_participant_failed(
        self, *, name: str, correlation_id: str, phase: TccPhase, participant_id: str, error: BaseException
    ) -> None: ...
    async def on_workflow_suspended(self, *, name: str, correlation_id: str, reason: str) -> None: ...
    async def on_workflow_resumed(self, *, name: str, correlation_id: str) -> None: ...
    async def on_signal_delivered(self, *, name: str, correlation_id: str, signal: str) -> None: ...
    async def on_timer_fired(self, *, name: str, correlation_id: str, timer_id: str) -> None: ...
    async def on_child_workflow_started(
        self, *, parent: str, correlation_id: str, child_workflow: str, child_correlation: str
    ) -> None: ...
    async def on_child_workflow_completed(
        self, *, parent: str, correlation_id: str, child_workflow: str, success: bool
    ) -> None: ...
    async def on_continue_as_new(self, *, name: str, correlation_id: str, new_correlation_id: str) -> None: ...
    async def on_dead_lettered(
        self, *, name: str, correlation_id: str, step_id: str | None, error: BaseException
    ) -> None: ...


class _BaseOrchestrationEvents:
    """No-op base class so concrete adapters only override what they care about.

    Method signatures intentionally mirror :class:`OrchestrationEvents` exactly,
    so subclasses that override individual hooks satisfy mypy's strict
    Liskov-substitution checks.
    """

    async def on_start(self, *, name: str, pattern: ExecutionPattern, correlation_id: str) -> None: ...
    async def on_completed(
        self,
        *,
        name: str,
        pattern: ExecutionPattern,
        correlation_id: str,
        success: bool,
        duration_ms: float,
    ) -> None: ...
    async def on_step_started(self, *, name: str, correlation_id: str, step_id: str) -> None: ...
    async def on_step_success(
        self, *, name: str, correlation_id: str, step_id: str, attempts: int, latency_ms: float
    ) -> None: ...
    async def on_step_failed(
        self,
        *,
        name: str,
        correlation_id: str,
        step_id: str,
        error: BaseException,
        attempts: int,
        latency_ms: float,
    ) -> None: ...
    async def on_step_skipped(self, *, name: str, correlation_id: str, step_id: str) -> None: ...
    async def on_compensation_started(self, *, name: str, correlation_id: str) -> None: ...
    async def on_step_compensated(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException | None
    ) -> None: ...
    async def on_phase_started(self, *, name: str, correlation_id: str, phase: TccPhase) -> None: ...
    async def on_phase_completed(
        self, *, name: str, correlation_id: str, phase: TccPhase, duration_ms: float
    ) -> None: ...
    async def on_phase_failed(
        self, *, name: str, correlation_id: str, phase: TccPhase, error: BaseException
    ) -> None: ...
    async def on_participant_started(
        self, *, name: str, correlation_id: str, phase: TccPhase, participant_id: str
    ) -> None: ...
    async def on_participant_success(
        self, *, name: str, correlation_id: str, phase: TccPhase, participant_id: str
    ) -> None: ...
    async def on_participant_failed(
        self,
        *,
        name: str,
        correlation_id: str,
        phase: TccPhase,
        participant_id: str,
        error: BaseException,
    ) -> None: ...
    async def on_workflow_suspended(self, *, name: str, correlation_id: str, reason: str) -> None: ...
    async def on_workflow_resumed(self, *, name: str, correlation_id: str) -> None: ...
    async def on_signal_delivered(self, *, name: str, correlation_id: str, signal: str) -> None: ...
    async def on_timer_fired(self, *, name: str, correlation_id: str, timer_id: str) -> None: ...
    async def on_child_workflow_started(
        self, *, parent: str, correlation_id: str, child_workflow: str, child_correlation: str
    ) -> None: ...
    async def on_child_workflow_completed(
        self, *, parent: str, correlation_id: str, child_workflow: str, success: bool
    ) -> None: ...
    async def on_continue_as_new(self, *, name: str, correlation_id: str, new_correlation_id: str) -> None: ...
    async def on_dead_lettered(
        self, *, name: str, correlation_id: str, step_id: str | None, error: BaseException
    ) -> None: ...


class LoggerOrchestrationEvents(_BaseOrchestrationEvents):
    """Default ``OrchestrationEvents`` implementation: logs to SLF4J-style logger."""

    async def on_start(self, *, name: str, pattern: ExecutionPattern, correlation_id: str) -> None:
        _logger.info("[%s/%s] %s started", pattern.value, correlation_id, name)

    async def on_completed(
        self, *, name: str, pattern: ExecutionPattern, correlation_id: str, success: bool, duration_ms: float
    ) -> None:
        outcome = "completed" if success else "failed"
        _logger.info("[%s/%s] %s %s in %.2fms", pattern.value, correlation_id, name, outcome, duration_ms)

    async def on_step_failed(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException, attempts: int, latency_ms: float
    ) -> None:
        _logger.warning(
            "[%s] step %s.%s failed after %d attempt(s) in %.2fms: %s",
            correlation_id,
            name,
            step_id,
            attempts,
            latency_ms,
            error,
        )

    async def on_step_compensated(
        self, *, name: str, correlation_id: str, step_id: str, error: BaseException | None
    ) -> None:
        if error is None:
            _logger.info("[%s] %s.%s compensated", correlation_id, name, step_id)
        else:
            _logger.error("[%s] compensation for %s.%s FAILED: %s", correlation_id, name, step_id, error)

    async def on_dead_lettered(
        self, *, name: str, correlation_id: str, step_id: str | None, error: BaseException
    ) -> None:
        _logger.error("[%s] %s/%s dead-lettered: %s", correlation_id, name, step_id, error)


class CompositeOrchestrationEvents(_BaseOrchestrationEvents):
    """Multiplexes a single event call to many delegate listeners."""

    def __init__(self, delegates: list[OrchestrationEvents] | None = None) -> None:
        self._delegates: list[OrchestrationEvents] = list(delegates or [])

    def add(self, listener: OrchestrationEvents) -> None:
        self._delegates.append(listener)

    def remove(self, listener: OrchestrationEvents) -> None:
        if listener in self._delegates:
            self._delegates.remove(listener)

    async def _dispatch(self, method: str, **kwargs: Any) -> None:
        for d in self._delegates:
            handler = getattr(d, method, None)
            if handler is None:
                continue
            try:
                await handler(**kwargs)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("orchestration event listener %s.%s raised: %s", type(d).__name__, method, exc)


def _make_dispatcher(method_name: str) -> Any:
    async def dispatcher(self: CompositeOrchestrationEvents, **kwargs: Any) -> None:
        await self._dispatch(method_name, **kwargs)

    return dispatcher


for _name in (
    "on_start",
    "on_completed",
    "on_step_started",
    "on_step_success",
    "on_step_failed",
    "on_step_skipped",
    "on_compensation_started",
    "on_step_compensated",
    "on_phase_started",
    "on_phase_completed",
    "on_phase_failed",
    "on_participant_started",
    "on_participant_success",
    "on_participant_failed",
    "on_workflow_suspended",
    "on_workflow_resumed",
    "on_signal_delivered",
    "on_timer_fired",
    "on_child_workflow_started",
    "on_child_workflow_completed",
    "on_continue_as_new",
    "on_dead_lettered",
):
    setattr(CompositeOrchestrationEvents, _name, _make_dispatcher(_name))
