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

"""OpenTelemetry tracer integration for Firefly Agentic.

:class:`AgentTracer` wraps the OpenTelemetry tracer with convenience
methods for creating agent- and tool-scoped spans.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from pyfly.agentic._runtime_context import RuntimeDefault

_TRACER_NAME = "pyfly.agentic"


class AgentTracer:
    """High-level tracer that creates spans with Firefly-specific attributes.

    Parameters:
        service_name: OpenTelemetry instrumentation scope name.
        tracer_provider: Application-owned provider; the API default when omitted.
        include_content: Whether exception messages and stack traces may be recorded.
    """

    def __init__(
        self, service_name: str = _TRACER_NAME, *, tracer_provider: Any = None, include_content: bool = False
    ) -> None:
        self._tracer: Tracer = trace.get_tracer(service_name, tracer_provider=tracer_provider)
        self._include_content = include_content

    @contextmanager
    def _span(self, name: str, attributes: dict[str, Any]) -> Generator[Span]:
        with self._tracer.start_as_current_span(
            name, attributes=attributes, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except BaseException as exc:
                self.set_error(span, exc, include_content=self._include_content)
                raise

    @contextmanager
    def agent_span(self, agent_name: str, *, model: str = "", **attributes: Any) -> Generator[Span]:
        """Create a span for an agent run."""
        with self._span(
            f"agent.{agent_name}",
            attributes={
                "pyfly.agentic.agent.name": agent_name,
                "pyfly.agentic.agent.model": model,
                **{f"pyfly.agentic.{k}": str(v) for k, v in attributes.items()},
            },
        ) as span:
            yield span

    @contextmanager
    def tool_span(self, tool_name: str, **attributes: Any) -> Generator[Span]:
        """Create a span for a tool execution."""
        with self._span(
            f"tool.{tool_name}",
            attributes={
                "pyfly.agentic.tool.name": tool_name,
                **{f"pyfly.agentic.{k}": str(v) for k, v in attributes.items()},
            },
        ) as span:
            yield span

    @contextmanager
    def reasoning_span(self, pattern_name: str, step: int = 0, **attributes: Any) -> Generator[Span]:
        """Create a span for a reasoning step."""
        with self._span(
            f"reasoning.{pattern_name}.step_{step}",
            attributes={
                "pyfly.agentic.reasoning.pattern": pattern_name,
                "pyfly.agentic.reasoning.step": step,
                **{f"pyfly.agentic.{k}": str(v) for k, v in attributes.items()},
            },
        ) as span:
            yield span

    @contextmanager
    def custom_span(self, name: str, **attributes: Any) -> Generator[Span]:
        """Create a span with arbitrary attributes."""
        with self._span(
            name,
            attributes={f"pyfly.agentic.{k}": str(v) for k, v in attributes.items()},
        ) as span:
            yield span

    def event(self, name: str, **attributes: Any) -> None:
        """Attach an instantaneous event to the currently active span.

        Use this for zero-duration annotations (e.g. routing decisions,
        cache hits, retries) instead of opening an empty span. No-op when
        no span is active.
        """
        span = trace.get_current_span()
        if span is None:
            return
        span.add_event(
            name,
            attributes={f"pyfly.agentic.{k}": str(v) for k, v in attributes.items()},
        )

    @staticmethod
    def set_error(span: Span, error: BaseException, *, include_content: bool = False) -> None:
        """Mark an error, withholding messages and stack traces unless content capture is enabled."""
        span.set_status(Status(StatusCode.ERROR, str(error) if include_content else None))
        if include_content:
            span.record_exception(error)
        else:
            error_type = type(error)
            name = error_type.__qualname__
            if error_type.__module__ != "builtins":
                name = f"{error_type.__module__}.{name}"
            span.add_event("exception", attributes={"exception.type": name})


# Module-level default tracer
default_tracer = cast(AgentTracer, RuntimeDefault[AgentTracer]("tracer", AgentTracer))
