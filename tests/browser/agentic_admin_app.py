"""A local native Agentic dashboard with deterministic, offline model usage."""

from contextlib import asynccontextmanager

from pydantic_ai.models.test import TestModel
from starlette.applications import Starlette

from pyfly.admin.wiring import build_admin_routes
from pyfly.agentic import AgenticConfig, AgenticRuntime, AgentTool
from pyfly.agentic.workflows.registry import Workflow
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config

runtime = AgenticRuntime(
    AgenticConfig(
        plugin_auto_discover=False, quota_enabled=True, quota_rate_limits={"test": 60}, quota_budget_daily_usd=5
    )
)
ctx = ApplicationContext(Config({"pyfly": {"app": {"name": "Research workspace"}}}))
ctx.container.register_instance(AgenticRuntime, runtime)


class Lookup(AgentTool):
    def __init__(self):
        super().__init__("knowledge_lookup", tags=["research", "internal"])

    async def _execute(self, **kwargs):
        return "Verified source"


@asynccontextmanager
async def lifespan(app):
    await runtime.start()
    agent = runtime.agent_factory.create(
        "research_assistant",
        model=TestModel(),
        description="Internal source research",
        tags=["research"],
        version="1.0",
    )
    runtime.tool_registry.register(Lookup())
    with runtime.activate():

        async def review(args):
            return args

        runtime.workflow_registry.register("source_review", Workflow("source_review", review))
    await agent.run("Demonstrate local usage")
    try:
        yield
    finally:
        await runtime.aclose()


app = Starlette(
    routes=build_admin_routes(ctx, admin_trace_collector=None, base_health_agg=None, extra_post_start=[]),
    lifespan=lifespan,
)
