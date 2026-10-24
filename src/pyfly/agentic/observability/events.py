# Copyright 2026 Firefly Software Foundation
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

"""Structured event logging for Firefly Agentic operations.

:class:`AgentEvents` emits structured log records for agent runs,
tool executions, reasoning steps, and lifecycle transitions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, Field

from pyfly.agentic._runtime_context import RuntimeDefault
from pyfly.context.events import ApplicationEventPublisher
from pyfly.eda.ports.outbound import EventPublisher

logger = logging.getLogger("pyfly.agentic.events")


class AgentEvent(BaseModel):
    """A structured event emitted by the framework."""

    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent: str = ""
    tool: str = ""
    pattern: str = ""
    detail: dict[str, Any] = {}
    level: str = "INFO"


class AgentEvents:
    """Emits structured events via Python logging.

    Events are logged as JSON-serialisable dicts so they integrate with
    any log aggregation backend.
    """

    def __init__(
        self,
        publisher: ApplicationEventPublisher | None = None,
        *,
        eda_publisher: EventPublisher | None = None,
        eda_destination: str | None = None,
    ) -> None:
        if (eda_publisher is None) != (eda_destination is None):
            raise ValueError("EDA export requires both eda_publisher and eda_destination")
        self._dispatcher: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]] | None = None
        self._publisher = publisher
        self._eda_publisher = eda_publisher
        self._eda_destination = eda_destination

    async def apublish(self, event: AgentEvent) -> None:
        """Await host listeners in the caller's transaction; telemetry failures are best effort."""
        from pyfly.agentic._runtime_context import selected_runtime

        runtime = selected_runtime()
        dispatcher = self._dispatcher
        if dispatcher is None and runtime is not None and runtime.events is self:
            dispatcher = runtime.on_application_loop
        if dispatcher is None:
            await self._apublish(event)
        else:
            await dispatcher(lambda: self._apublish(event))

    async def _apublish(self, event: AgentEvent) -> None:
        from pyfly.observability.correlation import current_correlation_context

        self._emit(event)
        if self._publisher is not None:
            try:
                await self._publisher.publish(event)
            except Exception:
                logger.warning("Application Agentic event listener failed", exc_info=True)
        if self._eda_publisher is not None and self._eda_destination is not None:
            try:
                await self._eda_publisher.publish(
                    self._eda_destination,
                    event.event_type,
                    event.model_dump(mode="json"),
                    headers=current_correlation_context(),
                )
            except Exception:
                logger.warning("Agentic EDA export failed", exc_info=True)

    async def aagent_started(self, agent_name: str, model: str = "", **extra: Any) -> None:
        await self.apublish(AgentEvent(event_type="agent.started", agent=agent_name, detail={"model": model, **extra}))

    async def aagent_completed(self, agent_name: str, *, tokens: int = 0, latency_ms: float = 0, **extra: Any) -> None:
        await self.apublish(
            AgentEvent(
                event_type="agent.completed",
                agent=agent_name,
                detail={"tokens": tokens, "latency_ms": latency_ms, **extra},
            )
        )

    async def aagent_error(self, agent_name: str, error: str, **extra: Any) -> None:
        await self.apublish(
            AgentEvent(event_type="agent.error", agent=agent_name, level="ERROR", detail={"error": error, **extra})
        )

    async def atool_executed(
        self, tool_name: str, *, success: bool = True, latency_ms: float = 0, **extra: Any
    ) -> None:
        await self.apublish(
            AgentEvent(
                event_type="tool.executed",
                tool=tool_name,
                detail={"success": success, "latency_ms": latency_ms, **extra},
            )
        )

    async def areasoning_step(self, pattern: str, step: int, step_type: str = "", **extra: Any) -> None:
        await self.apublish(
            AgentEvent(
                event_type="reasoning.step", pattern=pattern, detail={"step": step, "step_type": step_type, **extra}
            )
        )

    def agent_started(self, agent_name: str, model: str = "", **extra: Any) -> None:
        """Emit an event when an agent run starts."""
        self._emit(
            AgentEvent(
                event_type="agent.started",
                agent=agent_name,
                detail={"model": model, **extra},
            )
        )

    def agent_completed(self, agent_name: str, *, tokens: int = 0, latency_ms: float = 0, **extra: Any) -> None:
        """Emit an event when an agent run completes."""
        self._emit(
            AgentEvent(
                event_type="agent.completed",
                agent=agent_name,
                detail={"tokens": tokens, "latency_ms": latency_ms, **extra},
            )
        )

    def agent_error(self, agent_name: str, error: str, **extra: Any) -> None:
        """Emit an event when an agent run fails."""
        self._emit(
            AgentEvent(
                event_type="agent.error",
                agent=agent_name,
                level="ERROR",
                detail={"error": error, **extra},
            )
        )

    def tool_executed(self, tool_name: str, *, success: bool = True, latency_ms: float = 0, **extra: Any) -> None:
        """Emit an event for a tool execution."""
        self._emit(
            AgentEvent(
                event_type="tool.executed",
                tool=tool_name,
                detail={"success": success, "latency_ms": latency_ms, **extra},
            )
        )

    def reasoning_step(self, pattern: str, step: int, step_type: str = "", **extra: Any) -> None:
        """Emit an event for a reasoning step."""
        self._emit(
            AgentEvent(
                event_type="reasoning.step",
                pattern=pattern,
                detail={"step": step, "step_type": step_type, **extra},
            )
        )

    def _emit(self, event: AgentEvent) -> None:
        """Log the event as a structured dict."""
        log_level = getattr(logging, event.level, logging.INFO)
        logger.log(log_level, "%s", event.model_dump())


# Module-level default
default_events = cast(AgentEvents, RuntimeDefault[AgentEvents]("events", AgentEvents))
