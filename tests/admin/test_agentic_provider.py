"""Application-scoped, read-only native Agentic dashboard integration."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.models.test import TestModel  # noqa: E402
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.admin.wiring import build_admin_routes
from pyfly.agentic import AgenticConfig, AgenticRuntime, AgentTool
from pyfly.context.application_context import ApplicationContext
from pyfly.context.request_context import RequestContext
from pyfly.core.config import Config
from pyfly.security.context import SecurityContext


def _context(runtime=None, *, require_auth=False):
    context = ApplicationContext(Config({"pyfly": {"admin": {"require-auth": require_auth}}}))
    if runtime is not None:
        context.container.register_instance(AgenticRuntime, runtime)
    return context


def _client(context):
    return TestClient(
        Starlette(
            routes=build_admin_routes(context, admin_trace_collector=None, base_health_agg=None, extra_post_start=[])
        )
    )


class Lookup(AgentTool):
    def __init__(self):
        super().__init__("lookup", description="Public lookup", tags=["search"])

    async def _execute(self, **kwargs):
        return "Found"


@pytest.mark.asyncio
async def test_real_execution_usage_and_discovery_are_owned_by_application():
    from pyfly.admin.providers.agentic_provider import AgenticProvider

    first = AgenticRuntime(
        AgenticConfig(
            plugin_auto_discover=False,
            quota_enabled=True,
            quota_rate_limits={"test": 10, "https://PRIVATE_CREDENTIAL@provider": 5},
        )
    )
    second = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    try:
        agent = first.agent_factory.create("research", model=TestModel(), instructions="PRIVATE SYSTEM PROMPT")
        second.agent_factory.create("other-app", model=TestModel())
        first.tool_registry.register(Lookup())
        from pyfly.agentic.workflows.registry import Workflow

        with first.activate():

            async def flow(args):
                return args

            first.workflow_registry.register("review", Workflow("review", flow))
        await agent.run("PRIVATE USER PROMPT")
        provider = AgenticProvider(_context(first))
        with second.activate():
            data = await provider.get_overview()
        assert [item["name"] for item in data["agents"]] == ["research"]
        assert [item["name"] for item in data["tools"]] == ["lookup"]
        assert data["workflows"] == [{"name": "review", "status": "registered"}]
        assert data["usage"]["total_requests"] >= 1
        assert data["usage"]["total_tokens"] > 0
        assert data["quota"]["rate_limits"][0]["remaining"] == 9
        detail = await provider.get_agent("research")
        assert detail["usage"]["total_tokens"] == data["usage"]["total_tokens"]
        assert await provider.get_agent("other-app") is None
        assert await provider.get_tool("unknown") is None
        assert (await provider.get_tool("lookup"))["parameter_count"] == 0
        serialized = json.dumps(data) + json.dumps(detail)
        assert "PRIVATE" not in serialized
        assert "instructions" not in serialized
        assert "correlation_id" not in serialized
        assert (await AgenticProvider(_context(second)).get_overview())["usage"]["total_requests"] == 0
        first_response = _client(_context(first)).get("/admin/api/agentic").json()
        second_response = _client(_context(second)).get("/admin/api/agentic").json()
        assert [item["name"] for item in first_response["agents"]] == ["research"]
        assert [item["name"] for item in second_response["agents"]] == ["other-app"]
        assert second_response["usage"]["total_requests"] == 0
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_runtime_can_be_registered_after_admin_provider_creation():
    from pyfly.admin.providers.agentic_provider import AgenticProvider

    context = _context()
    provider = AgenticProvider(context)
    assert not provider.available
    assert await provider.get_overview() == {"available": False}
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    try:
        context.container.register_instance(AgenticRuntime, runtime)
        assert provider.available
        assert (await provider.get_overview())["agents"] == []
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_guarded_routes_details_missing_and_no_mutations():
    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False))
    try:
        runtime.agent_factory.create("research", model=TestModel())
        runtime.tool_registry.register(Lookup())
        client = _client(_context(runtime))
        assert client.get("/admin/api/settings").json()["agenticEnabled"] is True
        assert client.get("/admin/api/agentic").json()["available"] is True
        assert client.get("/admin/api/agentic/agents/research").json()["name"] == "research"
        assert client.get("/admin/api/agentic/tools/lookup").json()["name"] == "lookup"
        assert client.get("/admin/api/agentic/agents/missing").status_code == 404
        assert client.get("/admin/api/agentic/tools/missing").status_code == 404
        for method in (client.post, client.delete):
            assert method("/admin/api/agentic/agents/research").status_code == 405
        protected = _client(_context(runtime, require_auth=True))
        paths = ["/admin/api/agentic", "/admin/api/agentic/agents/research", "/admin/api/agentic/tools/lookup"]
        for path in paths:
            assert protected.get(path).status_code == 401
        rc = RequestContext.init()
        try:
            rc.security_context = SecurityContext(user_id="user", roles=["USER"])
            for path in paths:
                assert protected.get(path).status_code == 403
            rc.security_context = SecurityContext(user_id="admin", roles=["ADMIN"])
            for path in paths:
                assert protected.get(path).status_code == 200
        finally:
            RequestContext.clear()
    finally:
        await runtime.aclose()
