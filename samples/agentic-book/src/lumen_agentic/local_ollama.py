"""Explicit local-model opt-in, separate from the offline companion tests."""

import asyncio

from pyfly.agentic import Agent, AgentFactory, AgenticConfig, AgenticRuntime
from pyfly.agentic.models import ModelFactory, ModelSpec


async def build_local_assistant(factory: AgentFactory, model_name: str = "qwen2.5:3b") -> Agent:
    spec = ModelSpec(
        provider="ollama",
        model=model_name,
        base_url="http://127.0.0.1:11434/v1",
    )
    # A synthetic local token prevents inheriting a cloud credential from the environment.
    model = ModelFactory().build_with_secret(spec, "local-no-auth")
    factory.runtime.own(model.client)
    return factory.create(
        "lumen-local-assistant",
        model=model,
        instructions="Explain fictional Lumen policy. Never authorize a payment.",
        model_settings={"max_tokens": 128},
    )


async def main() -> None:
    async with AgenticRuntime(config=AgenticConfig(observability_enabled=False)) as runtime:
        assistant = await build_local_assistant(runtime.agent_factory)
        result = await assistant.run("Explain why a wallet review needs a human decision.", timeout=120)
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
