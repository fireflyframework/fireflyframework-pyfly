"""Native application restart journey shared by the persistence examples."""

from __future__ import annotations

from uuid import uuid4

from pydantic_ai.models.test import TestModel

from pyfly.agentic import AgentFactory
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.context import ApplicationContext
from pyfly.core.config import Config


async def exercise_persistence(config: Config) -> None:
    scope = f"native-example-{uuid4().hex}"
    context = ApplicationContext(config)
    await context.start()
    try:
        memory = context.get_bean(MemoryManager).fork(working_scope_id=scope)
        await memory.async_set_fact("project", {"name": "Lumen", "reviewers": 2})
        agent = context.get_bean(AgentFactory).create(
            "persistence-demo", memory=memory, model=TestModel(custom_output_text="Native memory is ready.")
        )
        conversation = memory.new_conversation()
        assert (await agent.run("Introduce the demo.", conversation_id=conversation)).output
        assert memory.get_message_history(conversation)
    finally:
        await context.stop()

    await context.start()
    try:
        restored = context.get_bean(MemoryManager).fork(working_scope_id=scope)
        assert await restored.async_get_fact("project") == {"name": "Lumen", "reviewers": 2}
        assert restored.get_message_history(conversation) == []
        await restored.working.async_clear()
    finally:
        await context.stop()
    print("Native working-memory facts survived a cold application restart.")
    print("Conversation history is process-local; export/import it explicitly when needed.")
