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

"""Observability subpackage -- tracing, metrics, events, and cost/usage tracking."""

from pyfly.agentic.observability.budget import (
    BudgetGate,
    BudgetMode,
    BudgetRule,
    BudgetWindow,
    ScopeContext,
)
from pyfly.agentic.observability.cost_resolvers import (
    DEFAULT_RESOLVERS,
    CostContext,
    UnknownModelCostError,
    genai_prices_cost,
    provider_reported_cost,
    resolve_cost,
)
from pyfly.agentic.observability.decorators import metered, traced
from pyfly.agentic.observability.events import AgentEvent, AgentEvents, default_events
from pyfly.agentic.observability.metrics import AgentMetrics, default_metrics
from pyfly.agentic.observability.pricing import ModelPriceQuote, quote_model_price
from pyfly.agentic.observability.sinks import (
    CostSink,
    EventBusSink,
    JSONLFileSink,
    LoggingSink,
    OTelMetricsSink,
)
from pyfly.agentic.observability.tracer import (
    AgentTracer,
    default_tracer,
)
from pyfly.agentic.observability.usage import (
    UsageRecord,
    UsageSummary,
    UsageTracker,
    default_usage_tracker,
)

__all__ = [
    "BudgetGate",
    "BudgetMode",
    "BudgetRule",
    "BudgetWindow",
    "CostContext",
    "CostSink",
    "DEFAULT_RESOLVERS",
    "EventBusSink",
    "AgentEvent",
    "AgentEvents",
    "AgentMetrics",
    "AgentTracer",
    "JSONLFileSink",
    "LoggingSink",
    "ModelPriceQuote",
    "OTelMetricsSink",
    "ScopeContext",
    "UnknownModelCostError",
    "UsageRecord",
    "UsageSummary",
    "UsageTracker",
    "default_events",
    "default_metrics",
    "default_tracer",
    "default_usage_tracker",
    "genai_prices_cost",
    "metered",
    "provider_reported_cost",
    "quote_model_price",
    "resolve_cost",
    "traced",
]
