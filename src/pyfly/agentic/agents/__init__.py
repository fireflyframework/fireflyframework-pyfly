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

"""Agents subpackage -- creation, registration, lifecycle, and delegation."""

from pyfly.agentic.agents.base import Agent, ApprovalHandler, is_deferred
from pyfly.agentic.agents.builtin_middleware import (
    CacheMiddleware,
    CostGuardMiddleware,
    ExplainabilityMiddleware,
    LoggingMiddleware,
    ObservabilityMiddleware,
    OutputGuardError,
    OutputGuardMiddleware,
    PromptGuardError,
    PromptGuardMiddleware,
    RetryMiddleware,
    ValidationMiddleware,
)
from pyfly.agentic.agents.cache import ResultCache
from pyfly.agentic.agents.context import AgentContext
from pyfly.agentic.agents.decorators import agent
from pyfly.agentic.agents.delegation import (
    Candidate,
    CapabilityStrategy,
    ChainStrategy,
    ContentBasedStrategy,
    CostAwareStrategy,
    DelegationRouter,
    DelegationStrategy,
    FallbackStrategy,
    RoundRobinStrategy,
    RoutingDecision,
    WeightedStrategy,
)
from pyfly.agentic.agents.fallback import FallbackModelWrapper, run_with_fallback
from pyfly.agentic.agents.lifecycle import AgentLifecycle, agent_lifecycle
from pyfly.agentic.agents.middleware import (
    AgentMiddleware,
    MiddlewareChain,
    MiddlewareContext,
)
from pyfly.agentic.agents.prompt_cache import CacheStatistics, PromptCacheMiddleware
from pyfly.agentic.agents.registry import AgentInfo, AgentRegistry, agent_registry
from pyfly.agentic.agents.templates import (
    create_classifier_agent,
    create_conversational_agent,
    create_extractor_agent,
    create_router_agent,
    create_summarizer_agent,
)
from pyfly.agentic.exceptions import BudgetExceededError

__all__ = [
    "AgentContext",
    "AgentInfo",
    "AgentLifecycle",
    "agent_lifecycle",
    "AgentMiddleware",
    "AgentRegistry",
    "ApprovalHandler",
    "BudgetExceededError",
    "CacheMiddleware",
    "CacheStatistics",
    "Candidate",
    "CapabilityStrategy",
    "ChainStrategy",
    "ContentBasedStrategy",
    "CostAwareStrategy",
    "CostGuardMiddleware",
    "DelegationRouter",
    "DelegationStrategy",
    "ExplainabilityMiddleware",
    "FallbackModelWrapper",
    "FallbackStrategy",
    "Agent",
    "is_deferred",
    "LoggingMiddleware",
    "MiddlewareChain",
    "MiddlewareContext",
    "ObservabilityMiddleware",
    "OutputGuardError",
    "OutputGuardMiddleware",
    "PromptGuardError",
    "PromptCacheMiddleware",
    "PromptGuardMiddleware",
    "ResultCache",
    "RetryMiddleware",
    "RoundRobinStrategy",
    "RoutingDecision",
    "ValidationMiddleware",
    "WeightedStrategy",
    "agent_registry",
    "create_classifier_agent",
    "create_conversational_agent",
    "create_extractor_agent",
    "create_router_agent",
    "create_summarizer_agent",
    "agent",
    "run_with_fallback",
]
