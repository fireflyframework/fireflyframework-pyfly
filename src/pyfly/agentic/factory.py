# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Application-bound agent construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.agentic.agents.base import Agent
    from pyfly.agentic.runtime import AgenticRuntime


class AgentFactory:
    def __init__(self, runtime: AgenticRuntime) -> None:
        self.runtime = runtime

    def create(self, name: str, **kwargs: Any) -> Agent[Any, Any]:
        from pyfly.agentic.agents.base import Agent

        kwargs.setdefault("runtime", self.runtime)
        kwargs.setdefault("memory", self.runtime.memory)
        with self.runtime.activate():
            return Agent(name, **kwargs)
