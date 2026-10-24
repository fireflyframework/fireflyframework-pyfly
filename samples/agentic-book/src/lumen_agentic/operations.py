"""The workflow uses real native agents backed by deterministic TestModel calls."""

from pydantic_ai.models.test import TestModel

from pyfly.agentic.workflows import Workflow, agent, phase


def assistance_workflow() -> Workflow:
    async def explain(question: str) -> str:
        with phase("retrieve"):
            policy = await agent(
                f"Retrieve policy for {question}",
                model=TestModel(
                    custom_output_text="Review requires a human decision.",
                ),
            )
        with phase("draft"):
            return await agent(
                f"Draft guidance from: {policy}",
                model=TestModel(
                    custom_output_text="Read the policy; request human review.",
                ),
            )

    return Workflow("lumen-assistance", explain)
