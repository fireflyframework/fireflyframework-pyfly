"""Typed read assistance and a separately approved review request."""

from collections.abc import MutableSequence

from pydantic import BaseModel
from pydantic_ai.models.test import TestModel

from pyfly.agentic.agents import Agent
from pyfly.agentic.factory import AgentFactory
from pyfly.agentic.tools import agent_tool
from pyfly.container import bean, configuration, service


class AssistanceReply(BaseModel):
    summary: str
    policy_id: str
    needs_human: bool


@agent_tool("lookup_policy", auto_register=False)
async def lookup_policy() -> str:
    """Read the fictional Lumen policy; never change wallet state."""
    return "lumen-review-v1: Review requires a human decision."


def build_assistant(factory: AgentFactory) -> Agent:
    return factory.create(
        "lumen-assistant",
        model=TestModel(
            custom_output_args={
                "summary": "Request human review.",
                "policy_id": "lumen-review-v1",
                "needs_human": True,
            }
        ),
        output_type=AssistanceReply,
        tools=[lookup_policy],
        instructions="Explain policy and cite its ID. Never authorize a payment.",
    )


@configuration
class AssistanceConfiguration:
    @bean
    def lumen_assistant(self, factory: AgentFactory) -> Agent:
        return build_assistant(factory)


@service
class AssistanceService:
    def __init__(self, assistant: Agent) -> None:
        self._assistant = assistant

    async def explain(self, question: str) -> AssistanceReply:
        result = await self._assistant.run(question)
        return AssistanceReply.model_validate(result.output)


def build_review_agent(factory: AgentFactory, effects: MutableSequence[str]) -> Agent:
    @agent_tool("request_review", requires_approval=True, auto_register=False)
    async def request_review() -> str:
        """Create a fictional review request after an authorized human approves."""
        effects.append("review-requested")
        return "review-requested"

    return factory.create(
        "lumen-review",
        model=TestModel(),
        tools=[request_review],
        hitl=True,
    )
