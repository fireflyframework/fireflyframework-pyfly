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

"""Firefly Agentic Framework -- A metaframework built on Pydantic AI.

This package provides production-grade abstractions for building GenAI
applications including agents, reasoning patterns, prompt engineering,
tools, observability, explainability, and experimentation.

Quick start::

    from pyfly.agentic import AgenticConfig, get_config

    config = get_config()
    print(config.default_model)
"""

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

try:
    __version__ = version("pyfly")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"

if TYPE_CHECKING:
    from pyfly.agentic.agents.base import Agent
    from pyfly.agentic.agents.decorators import agent
    from pyfly.agentic.config import AgenticConfig
    from pyfly.agentic.factory import AgentFactory
    from pyfly.agentic.runtime import AgenticRuntime
    from pyfly.agentic.tools.base import AgentTool
    from pyfly.agentic.tools.decorators import agent_tool

__all__ = [
    "Agent",
    "AgentTool",
    "AgenticRuntime",
    "AgentFactory",
    "agent",
    "agent_tool",
    "__version__",
    "AgenticConfig",
    "get_config",
    "on_config_installed",
    "reset_config",
    "set_config",
    "AgenticError",
    "AgenticMemoryError",
    "ConfigurationError",
    "AgentError",
    "AgentNotFoundError",
    "DelegationError",
    "ToolError",
    "ToolGuardError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "PromptError",
    "PromptNotFoundError",
    "PromptValidationError",
    "ReasoningError",
    "ReasoningStepLimitError",
    "ReasoningPatternNotFoundError",
    "ExperimentError",
    "ObservabilityError",
    "ExplainabilityError",
    "ChunkingError",
    "CompressionError",
    "OutputReviewError",
    "OutputValidationError",
    "PipelineError",
    "QoSError",
    "QuotaError",
    "BudgetExceededError",
    "RateLimitError",
    "DatabaseStoreError",
    "DatabaseConnectionError",
    "EmbeddingError",
    "EmbeddingProviderError",
    "VectorStoreError",
    "VectorStoreConnectionError",
    "MemoryError",
    "AgentDepsT",
    "AgentLike",
    "OutputT",
    "ToolInputT",
    "ToolOutputT",
    "JSON",
    "Metadata",
    "Headers",
    "UserContent",
    "MultiModalContent",
    "UserPrompt",
    "ImageUrl",
    "AudioUrl",
    "DocumentUrl",
    "VideoUrl",
    "BinaryContent",
    "BaseEmbedder",
    "EmbedderRegistry",
    "EmbeddingProtocol",
    "BaseVectorStore",
    "InMemoryVectorStore",
    "VectorStoreProtocol",
    "PluginDiscovery",
    "configure_logging",
    "enable_debug",
]


_EXPORTS = {
    "Agent": ("pyfly.agentic.agents.base", "Agent"),
    "AgentTool": ("pyfly.agentic.tools.base", "AgentTool"),
    "AgenticRuntime": ("pyfly.agentic.runtime", "AgenticRuntime"),
    "AgentFactory": ("pyfly.agentic.factory", "AgentFactory"),
    "agent": ("pyfly.agentic.agents.decorators", "agent"),
    "agent_tool": ("pyfly.agentic.tools.decorators", "agent_tool"),
    "AgenticConfig": ("pyfly.agentic.config", "AgenticConfig"),
    "get_config": ("pyfly.agentic.config", "get_config"),
    "on_config_installed": ("pyfly.agentic.config", "on_config_installed"),
    "reset_config": ("pyfly.agentic.config", "reset_config"),
    "set_config": ("pyfly.agentic.config", "set_config"),
    "BaseEmbedder": ("pyfly.agentic.embeddings", "BaseEmbedder"),
    "EmbedderRegistry": ("pyfly.agentic.embeddings", "EmbedderRegistry"),
    "EmbeddingProtocol": ("pyfly.agentic.embeddings", "EmbeddingProtocol"),
    "AgentError": ("pyfly.agentic.exceptions", "AgentError"),
    "AgentNotFoundError": ("pyfly.agentic.exceptions", "AgentNotFoundError"),
    "BudgetExceededError": ("pyfly.agentic.exceptions", "BudgetExceededError"),
    "ChunkingError": ("pyfly.agentic.exceptions", "ChunkingError"),
    "CompressionError": ("pyfly.agentic.exceptions", "CompressionError"),
    "ConfigurationError": ("pyfly.agentic.exceptions", "ConfigurationError"),
    "DatabaseConnectionError": ("pyfly.agentic.exceptions", "DatabaseConnectionError"),
    "DatabaseStoreError": ("pyfly.agentic.exceptions", "DatabaseStoreError"),
    "DelegationError": ("pyfly.agentic.exceptions", "DelegationError"),
    "EmbeddingError": ("pyfly.agentic.exceptions", "EmbeddingError"),
    "EmbeddingProviderError": ("pyfly.agentic.exceptions", "EmbeddingProviderError"),
    "ExperimentError": ("pyfly.agentic.exceptions", "ExperimentError"),
    "ExplainabilityError": ("pyfly.agentic.exceptions", "ExplainabilityError"),
    "AgenticError": ("pyfly.agentic.exceptions", "AgenticError"),
    "AgenticMemoryError": ("pyfly.agentic.exceptions", "AgenticMemoryError"),
    "MemoryError": ("pyfly.agentic.exceptions", "MemoryError"),
    "ObservabilityError": ("pyfly.agentic.exceptions", "ObservabilityError"),
    "OutputReviewError": ("pyfly.agentic.exceptions", "OutputReviewError"),
    "OutputValidationError": ("pyfly.agentic.exceptions", "OutputValidationError"),
    "PipelineError": ("pyfly.agentic.exceptions", "PipelineError"),
    "PromptError": ("pyfly.agentic.exceptions", "PromptError"),
    "PromptNotFoundError": ("pyfly.agentic.exceptions", "PromptNotFoundError"),
    "PromptValidationError": ("pyfly.agentic.exceptions", "PromptValidationError"),
    "QoSError": ("pyfly.agentic.exceptions", "QoSError"),
    "QuotaError": ("pyfly.agentic.exceptions", "QuotaError"),
    "RateLimitError": ("pyfly.agentic.exceptions", "RateLimitError"),
    "ReasoningError": ("pyfly.agentic.exceptions", "ReasoningError"),
    "ReasoningPatternNotFoundError": ("pyfly.agentic.exceptions", "ReasoningPatternNotFoundError"),
    "ReasoningStepLimitError": ("pyfly.agentic.exceptions", "ReasoningStepLimitError"),
    "ToolError": ("pyfly.agentic.exceptions", "ToolError"),
    "ToolGuardError": ("pyfly.agentic.exceptions", "ToolGuardError"),
    "ToolNotFoundError": ("pyfly.agentic.exceptions", "ToolNotFoundError"),
    "ToolTimeoutError": ("pyfly.agentic.exceptions", "ToolTimeoutError"),
    "VectorStoreConnectionError": ("pyfly.agentic.exceptions", "VectorStoreConnectionError"),
    "VectorStoreError": ("pyfly.agentic.exceptions", "VectorStoreError"),
    "configure_logging": ("pyfly.agentic.logging", "configure_logging"),
    "enable_debug": ("pyfly.agentic.logging", "enable_debug"),
    "PluginDiscovery": ("pyfly.agentic.plugin", "PluginDiscovery"),
    "JSON": ("pyfly.agentic.types", "JSON"),
    "AgentDepsT": ("pyfly.agentic.types", "AgentDepsT"),
    "AgentLike": ("pyfly.agentic.types", "AgentLike"),
    "AudioUrl": ("pyfly.agentic.types", "AudioUrl"),
    "BinaryContent": ("pyfly.agentic.types", "BinaryContent"),
    "DocumentUrl": ("pyfly.agentic.types", "DocumentUrl"),
    "Headers": ("pyfly.agentic.types", "Headers"),
    "ImageUrl": ("pyfly.agentic.types", "ImageUrl"),
    "Metadata": ("pyfly.agentic.types", "Metadata"),
    "MultiModalContent": ("pyfly.agentic.types", "MultiModalContent"),
    "OutputT": ("pyfly.agentic.types", "OutputT"),
    "ToolInputT": ("pyfly.agentic.types", "ToolInputT"),
    "ToolOutputT": ("pyfly.agentic.types", "ToolOutputT"),
    "UserContent": ("pyfly.agentic.types", "UserContent"),
    "UserPrompt": ("pyfly.agentic.types", "UserPrompt"),
    "VideoUrl": ("pyfly.agentic.types", "VideoUrl"),
    "BaseVectorStore": ("pyfly.agentic.vectorstores", "BaseVectorStore"),
    "InMemoryVectorStore": ("pyfly.agentic.vectorstores", "InMemoryVectorStore"),
    "VectorStoreProtocol": ("pyfly.agentic.vectorstores", "VectorStoreProtocol"),
}


def __getattr__(name: str) -> Any:
    from importlib import import_module

    if name in _EXPORTS:
        module, symbol = _EXPORTS[name]
        value = getattr(import_module(module), symbol)
        globals()[name] = value
        return value
    raise AttributeError(name)
