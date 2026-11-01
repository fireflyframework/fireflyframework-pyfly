# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Read-only Agentic introspection scoped to the owning application context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pyfly.agentic.runtime import AgenticRuntime
    from pyfly.context.application_context import ApplicationContext


class AgenticProvider:
    """Expose an allowlisted snapshot without importing optional AI dependencies."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    def _runtime(self) -> AgenticRuntime | None:
        # Resolve on each request: admin routes are built before bean startup.
        # Never consult RuntimeDefault proxies or the current execution context.
        for registration in self._context.container._registrations.values():
            instance = registration.instance
            if instance is not None and any(
                cls.__module__ == "pyfly.agentic.runtime" and cls.__name__ == "AgenticRuntime"
                for cls in type(instance).__mro__
            ):
                return cast("AgenticRuntime", instance)
        return None

    @property
    def available(self) -> bool:
        return self._runtime() is not None

    @staticmethod
    def _usage(summary: Any) -> dict[str, Any]:
        return {
            key: getattr(summary, key)
            for key in (
                "total_input_tokens",
                "total_output_tokens",
                "total_tokens",
                "total_cost_usd",
                "total_requests",
                "total_latency_ms",
                "record_count",
                "cost_status",
                "known_cost_usd",
                "known_cost_records",
                "unknown_cost_records",
            )
        }

    async def get_overview(self) -> dict[str, Any]:
        runtime = self._runtime()
        if runtime is None:
            return {"available": False}
        quota = runtime.quota_manager
        return {
            "available": True,
            "agents": [
                {"name": agent.name, "version": agent.version, "tags": list(agent.tags)}
                for agent in runtime.agent_registry.list_agents()
            ],
            "tools": [
                {"name": tool.name, "tags": list(tool.tags), "parameter_count": tool.parameter_count}
                for tool in runtime.tool_registry.list_tools()
            ],
            "workflows": [
                {"name": name, "status": "registered"} for name in runtime.workflow_registry.list_workflows()
            ],
            "usage": self._usage(runtime.usage_tracker.get_summary()),
            "quota": {
                "enabled": quota is not None,
                "daily_budget_usd": runtime.config.quota_budget_daily_usd,
                "rate_limits": [
                    {"limit": limit, "remaining": quota.get_rate_limit_remaining(model)}
                    for model, limit in runtime.config.quota_rate_limits.items()
                ]
                if quota is not None
                else [],
            },
        }

    async def get_agent(self, name: str) -> dict[str, Any] | None:
        runtime = self._runtime()
        if runtime is None:
            return None
        for agent in runtime.agent_registry.list_agents():
            if agent.name == name:
                return {
                    "name": agent.name,
                    "version": agent.version,
                    "tags": list(agent.tags),
                    "usage": self._usage(runtime.usage_tracker.get_summary_for_agent(name)),
                }
        return None

    async def get_tool(self, name: str) -> dict[str, Any] | None:
        runtime = self._runtime()
        if runtime is None:
            return None
        for tool in runtime.tool_registry.list_tools():
            if tool.name == name:
                return {"name": tool.name, "tags": list(tool.tags), "parameter_count": tool.parameter_count}
        return None
