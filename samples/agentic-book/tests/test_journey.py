"""Real native runtime + Pydantic AI TestModel, with no provider traffic."""

import pytest
from lumen_agentic.assistance import AssistanceReply, build_assistant, build_review_agent
from lumen_agentic.knowledge import build_knowledge
from lumen_agentic.operations import assistance_workflow
from pydantic_ai.models.test import TestModel

from pyfly.agentic.agents import is_deferred
from pyfly.agentic.config import AgenticConfig
from pyfly.agentic.exceptions import WorkflowBudgetError
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.agentic.tools import DeferredToolResults, ToolApproved
from pyfly.agentic.workflows import Journal, WorkflowBudget


def config():
    return AgenticConfig(observability_enabled=False)


@pytest.mark.asyncio
async def test_native_factory_returns_registered_typed_agent_and_calls_read_tool():
    async with AgenticRuntime(config=config()) as runtime:
        agent = build_assistant(runtime.agent_factory)
        result = await agent.run("Explain Lumen's wallet review policy.")
        assert isinstance(result.output, AssistanceReply)
        assert result.output.policy_id == "lumen-review-v1"
        assert runtime.agent_registry.get("lumen-assistant") is agent
        messages = str(result.all_messages())
        assert "lookup_policy" in messages
        assert "Review requires a human decision" in messages


@pytest.mark.asyncio
async def test_stream_context_delivers_complete_text():
    async with AgenticRuntime(config=config()) as runtime:
        agent = runtime.agent_factory.create(
            "lumen-stream",
            model=TestModel(custom_output_text="Review is pending."),
        )
        async with await agent.run_stream("Explain the status") as stream:
            chunks = [part async for part in stream.stream_text(delta=True)]
        assert "".join(chunks) == "Review is pending."


@pytest.mark.asyncio
async def test_human_approval_defers_effect_then_resumes_once():
    effects = []
    async with AgenticRuntime(config=config()) as runtime:
        agent = build_review_agent(runtime.agent_factory, effects)
        paused = await agent.run("Create a review request")
        assert is_deferred(paused)
        assert effects == []
        request = paused.output.approvals[0]
        result = await agent.run(
            None,
            message_history=paused.all_messages(),
            deferred_tool_results=DeferredToolResults(
                approvals={request.tool_call_id: ToolApproved()},
            ),
        )
        assert not is_deferred(result)
        assert effects == ["review-requested"]


@pytest.mark.asyncio
async def test_knowledge_scope_is_required_and_other_tenant_cannot_retrieve():
    store = await build_knowledge()
    results = await store.search([1.0, 0.0], tenant_id="lumen", workspace_id="help")
    assert results[0].document.id == "lumen-review-v1"
    assert await store.search([1.0, 0.0], tenant_id="other", workspace_id="help") == []
    with pytest.raises(TypeError):
        await store.search([1.0, 0.0])
    await store.close()


@pytest.mark.asyncio
async def test_workflow_journal_replay_and_budget_backstop():
    async with AgenticRuntime(config=config()) as runtime:
        with runtime.activate():
            journal = Journal()
            workflow = assistance_workflow()
            budget = WorkflowBudget(max_agents_total=2, max_concurrent_agents=1)
            first = await workflow.run("wallet review", journal=journal, budget=budget)
            assert first == "Read the policy; request human review."
            assert len(journal) == 2

            class UnexpectedCallRunner:
                async def run(self, *args, **kwargs):
                    raise AssertionError("journal replay attempted another model call")

            replay = await workflow.run(
                "wallet review",
                journal=journal,
                budget=budget,
                runner=UnexpectedCallRunner(),
            )
            assert replay == first
            with pytest.raises(WorkflowBudgetError):
                await workflow.run("wallet review", budget=WorkflowBudget(max_agents_total=1))


@pytest.mark.asyncio
async def test_application_context_injects_lumen_service_and_stops_runtime():
    from lumen_agentic.assistance import AssistanceConfiguration, AssistanceService

    from pyfly.agentic.factory import AgentFactory
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "agentic": {
                        "enabled": True,
                        "observability-enabled": False,
                        "plugin-auto-discover": False,
                    }
                }
            }
        )
    )
    ctx.register_bean(AssistanceConfiguration)
    ctx.register_bean(AssistanceService)
    await ctx.start()
    try:
        runtime = ctx.get_bean(AgenticRuntime)
        service = ctx.get_bean(AssistanceService)
        reply = await service.explain("Explain review policy")
        assert reply.policy_id == "lumen-review-v1"
        assert ctx.get_bean(AgentFactory) is runtime.agent_factory
        assert service._assistant is runtime.agent_registry.get("lumen-assistant")
    finally:
        await ctx.stop()
    with pytest.raises(RuntimeError, match="closed"), runtime.activate():
        pass


@pytest.mark.asyncio
async def test_local_ollama_configuration_builds_without_a_model_request():
    from lumen_agentic.local_ollama import build_local_assistant

    async with AgenticRuntime(config=config()) as runtime:
        assistant = await build_local_assistant(runtime.agent_factory, model_name="qwen2.5:3b")
        assert runtime.agent_registry.get("lumen-local-assistant") is assistant
