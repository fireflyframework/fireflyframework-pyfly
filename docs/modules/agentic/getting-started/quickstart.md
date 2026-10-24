---
title: Quick Start
description: Run a complete PyFly Agentic agent with a tool, conversation memory and typed model options.
---

# 5-Minute Quick Start

[Install PyFly Agentic](installation.md) in a Python 3.12+ environment first. This guide
runs a complete application; the [visual guide](../diagrams.md) explains the path
from your agent to the selected provider.

For local inference, follow [Local Ollama](ollama.md).

## 1. Configure the Model

Create a `.env` file beside your script:

```dotenv
OPENAI_API_KEY=your-api-key
PYFLY_AGENTIC_DEFAULT_MODEL=openai-responses:gpt-6-luna
```

Keep `.env` out of version control. To use another provider, change the model
selector and supply that provider's credentials. Choose a model that supports tool
calling. The script below loads `.env`; existing environment variables take
precedence.

## 2. Run an Agent with a Tool and Memory

Save this complete script as `app.py`. It uses PyFly's runtime, factory, tools, memory and typed model options:

```python
import asyncio

from dotenv import load_dotenv

load_dotenv()

from pyfly.agentic.auto_configuration import bind_agentic_config
from pyfly.core import Config
from pyfly.agentic.runtime import AgenticRuntime
from pyfly.agentic.memory import MemoryManager
from pyfly.agentic.models import ModelOptions
from pyfly.agentic.tools import agent_tool

GLOSSARY = {
    "bounded retries": "Retry a failed operation up to a configured attempt limit.",
    "backoff": "Increase the delay between retry attempts to reduce load.",
}


@agent_tool("lookup", auto_register=False)
async def lookup(term: str) -> str:
    """Look up an engineering term in the local glossary."""
    return GLOSSARY.get(term.strip().lower(), "No glossary entry exists for that term.")


async def main() -> None:
    async with AgenticRuntime(config=bind_agentic_config(Config())) as runtime:
        with runtime.activate():
            memory = MemoryManager()
            agent = runtime.agent_factory.create(
                name="glossary",
                instructions="Use the lookup tool for glossary definitions. Keep answers brief.",
                tools=[lookup],
                memory=memory,
                model_options=ModelOptions(max_tokens=4096),
                auto_register=False,
            )
            conversation_id = memory.new_conversation()

            first = await agent.run(
                "Use the glossary to explain bounded retries.",
                conversation_id=conversation_id,
            )
            print(first.output)

            follow_up = await agent.run(
                "Which term did I ask you to explain?",
                conversation_id=conversation_id,
                model_options=ModelOptions(max_tokens=2048),
            )
            print(follow_up.output)


if __name__ == "__main__":
    asyncio.run(main())
```

```bash
python app.py
```

This makes real provider requests. `tools=[lookup]` attaches the tool to the agent;
reusing `conversation_id` preserves the conversation. The runtime closes owned resources on exit. Changing the configured model
or supported API does not require rewriting the tool or memory code.

For the configured GPT-6 Luna Responses model, add `reasoning="low"` to
`ModelOptions` when reasoning is useful. Other models and APIs have their own
reasoning/tool constraints; consult the model compatibility guide.
Responses-specific storage is controlled with `store_responses=False`; these
options are validated against the selected model and API. See the
[Responses example](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/openai_responses.py) for reasoning, structured output,
and streaming with those controls.

## 3. Extend the Same Application

| Add | Guide or runnable example |
|---|---|
| Plain Pydantic output schemas and streaming | [Model-agnostic agent](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/model_agnostic_agent.py) |
| Decorator-defined agents | [Agent decorators](../agents.md#using-the-decorator) |
| Tool approval and deferred runs | [Human-in-the-loop tools](../tools.md#human-in-the-loop-tool-approval) |
| Persistent working memory and conversation export/import | [Memory](../memory.md) |
| Reasoning patterns | [Reasoning](../reasoning.md) |
| Output validation and review | [Validation](../validation.md) |
| Multi-agent pipelines | [Pipeline](../pipeline.md) |
| Embeddings and retrieval | [Vector stores](../vectorstores.md) |

Browse the [example catalogue](https://github.com/fireflyframework/fireflyframework-pyfly/blob/main/examples/agentic/catalogue.md) for setup requirements,
credentials, and offline or live execution modes.


Next: follow the [complete tutorial](../tutorial.md), or compare
[Chat Completions and Responses](../models.md#openai-choose-the-api-explicitly).
