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

"""Metrics collection for Firefly Agentic agents.

:class:`AgentMetrics` uses the OpenTelemetry metrics API to record
token usage, latency, cost estimates, and error rates.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from opentelemetry import metrics

from pyfly.agentic._runtime_context import RuntimeDefault

_METER_NAME = "pyfly.agentic"


class AgentMetrics:
    """Collects agent, tool, and reasoning metrics via OpenTelemetry.

    Parameters:
        service_name: Meter identity.
    """

    def __init__(self, service_name: str = _METER_NAME, *, meter_provider: Any = None) -> None:
        meter = metrics.get_meter(service_name, meter_provider=meter_provider)

        self.token_counter = meter.create_counter(
            "pyfly.agentic.tokens.total",
            description="Total tokens consumed (prompt + completion)",
            unit="token",
        )
        self.latency_histogram = meter.create_histogram(
            "pyfly.agentic.latency",
            description="Latency of agent/tool/reasoning operations",
            unit="ms",
        )
        self.error_counter = meter.create_counter(
            "pyfly.agentic.errors.total",
            description="Total errors across agents, tools, and reasoning",
        )
        self.cost_counter = meter.create_counter(
            "pyfly.agentic.cost.total",
            description="Estimated cost in USD",
            unit="usd",
        )
        self.cost_observation_counter = meter.create_counter(
            "pyfly.agentic.cost.observations",
            description="Usage observations with known or unknown estimated cost",
            unit="{observation}",
        )
        self.prompt_token_counter = meter.create_counter(
            "pyfly.agentic.tokens.prompt",
            description="Input/prompt tokens consumed",
            unit="token",
        )
        self.completion_token_counter = meter.create_counter(
            "pyfly.agentic.tokens.completion",
            description="Output/completion tokens consumed",
            unit="token",
        )
        self.reasoning_depth = meter.create_histogram(
            "pyfly.agentic.reasoning.depth",
            description="Number of steps taken by reasoning patterns",
            unit="step",
        )

    def record_tokens(self, count: int, *, agent: str = "", model: str = "") -> None:
        """Record token usage."""
        self.token_counter.add(count, {"agent": agent, "model": model})

    def record_latency(self, ms: float, *, operation: str = "", agent: str = "") -> None:
        """Record operation latency in milliseconds."""
        self.latency_histogram.record(ms, {"operation": operation, "agent": agent})

    def record_error(self, *, operation: str = "", agent: str = "", error_type: str = "") -> None:
        """Increment error counter."""
        self.error_counter.add(1, {"operation": operation, "agent": agent, "error_type": error_type})

    def record_cost(self, usd: float, *, agent: str = "", model: str = "") -> None:
        """Record estimated cost."""
        self.cost_counter.add(usd, {"agent": agent, "model": model})

    def record_cost_observation(self, status: Literal["known", "unknown"]) -> None:
        if status not in {"known", "unknown"}:
            raise ValueError("Unsupported cost coverage status")
        self.cost_observation_counter.add(1, {"status": status})

    def record_prompt_tokens(self, count: int, *, agent: str = "", model: str = "") -> None:
        """Record input/prompt token usage."""
        self.prompt_token_counter.add(count, {"agent": agent, "model": model})

    def record_completion_tokens(self, count: int, *, agent: str = "", model: str = "") -> None:
        """Record output/completion token usage."""
        self.completion_token_counter.add(count, {"agent": agent, "model": model})

    def record_reasoning_depth(self, steps: int, *, pattern: str = "") -> None:
        """Record reasoning step count."""
        self.reasoning_depth.record(steps, {"pattern": pattern})


# Module-level default
default_metrics = cast(AgentMetrics, RuntimeDefault[AgentMetrics]("metrics", AgentMetrics))
