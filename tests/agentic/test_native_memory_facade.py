"""Native memory awaits persistence and authenticates typed payloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pyfly.agentic import AgenticConfig
from pyfly.agentic.memory.manager import MemoryManager
from pyfly.agentic.memory.store import InMemoryStore
from pyfly.agentic.memory.types import MemoryEntry
from pyfly.agentic.reasoning.chain_of_thought import ChainOfThoughtPattern
from pyfly.agentic.reasoning.models import ReasoningThought
from pyfly.agentic.security.encryption import AESEncryptionProvider, EncryptedMemoryStore


class AsyncOnlyStore:
    def __init__(self):
        self.entries = {}
        self.owner = asyncio.get_running_loop()
        self.replacements = 0
        self.initializations = self.closes = 0

    def _sync(self, *args):
        raise AssertionError("Native asynchronous consumer called synchronous persistence")

    save = load = load_by_key = delete = clear = _sync

    async def initialize(self):
        self.initializations += 1

    async def close(self):
        self.closes += 1

    async def async_save(self, namespace, entry):
        assert asyncio.get_running_loop() is self.owner
        self.entries[(namespace, entry.entry_id)] = entry

    async def async_load(self, namespace):
        return [entry for (scope, _), entry in self.entries.items() if scope == namespace]

    async def async_load_by_key(self, namespace, key):
        return next((entry for entry in await self.async_load(namespace) if entry.key == key), None)

    async def async_delete(self, namespace, entry_id):
        self.entries.pop((namespace, entry_id), None)

    async def async_clear(self, namespace):
        self.entries = {key: value for key, value in self.entries.items() if key[0] != namespace}

    async def async_replace(self, namespace, entry):
        self.replacements += 1
        existing = await self.async_load_by_key(namespace, entry.key)
        if existing is not None:
            await self.async_delete(namespace, existing.entry_id)
        await self.async_save(namespace, entry)


async def test_async_facade_uses_atomic_backend_and_keeps_fork_scope():
    store = AsyncOnlyStore()
    manager = MemoryManager.from_config(AgenticConfig(), store=store, initialize=False)
    child = manager.fork(working_scope_id="child")
    await manager.async_set_fact("policy", {"revision": 1})
    await manager.async_set_fact("policy", {"revision": 2})
    await child.async_set_fact("policy", ["child"])
    assert await manager.async_get_fact("policy") == {"revision": 2}
    assert await child.async_get_fact("policy") == ["child"]
    assert await manager.working.async_keys() == ["policy"]
    assert "revision" in await manager.async_get_working_context()
    assert store.replacements == 3
    await manager.working.async_clear()
    assert await manager.async_get_fact("policy", "absent") == "absent"
    assert await child.async_get_fact("policy") == ["child"]


async def test_reasoning_persists_through_async_memory_facade():
    class Model:
        async def run(self, prompt, **kwargs):
            return SimpleNamespace(output=ReasoningThought(content="done", is_final=True, final_answer="42"))

    store = AsyncOnlyStore()
    result = await ChainOfThoughtPattern(max_steps=2).execute(
        Model(), "Answer locally", memory=MemoryManager(store=store)
    )
    assert result.success
    assert store.replacements > 0


@pytest.mark.parametrize("content", [{"amount": [1, False, None]}, ["a", {"nested": 2}], 42, False, None, "hello"])
def test_configured_encryption_round_trips_json_types(content):
    raw = InMemoryStore()
    manager = MemoryManager.from_config(
        AgenticConfig(encryption_enabled=True, encryption_key="a" * 32), store=raw, initialize=False
    )
    manager.set_fact("typed", content)
    encoded = raw.load_by_key("working:default", "typed")
    assert encoded is not None
    if content is not None:
        assert encoded.content != content
    result = manager.get_fact("typed")
    assert result == content
    assert type(result) is type(content)


def test_enabled_encryption_without_key_fails_before_memory_is_used():
    with pytest.raises(ValueError, match="encryption_key"):
        MemoryManager.from_config(AgenticConfig(encryption_enabled=True), initialize=False)


def test_bad_memory_ciphertext_fails_closed_and_preserves_stored_value():
    raw = InMemoryStore()
    good = EncryptedMemoryStore(raw, encryption_key="a" * 32)
    good.save("scope", MemoryEntry(entry_id="fixed", content={"secret": "fictional"}))
    ciphertext = raw.load("scope")[0].content
    with pytest.raises(ValueError, match="decrypt|Decryption"):
        EncryptedMemoryStore(raw, encryption_key="b" * 32).load("scope")
    assert raw.load("scope")[0].content == ciphertext


async def test_async_encryption_preserves_backend_operations_and_lifecycle():
    raw = AsyncOnlyStore()
    encrypted = EncryptedMemoryStore(raw, provider=AESEncryptionProvider("a" * 32))
    await encrypted.initialize()
    await encrypted.async_replace("scope", MemoryEntry(key="typed", content={"amount": 2}))
    assert (await encrypted.async_load_by_key("scope", "typed")).content == {"amount": 2}
    assert (await raw.async_load("scope"))[0].content != {"amount": 2}
    await encrypted.async_delete("scope", (await raw.async_load("scope"))[0].entry_id)
    assert await encrypted.async_load("scope") == []
    await encrypted.aclose()
    assert raw.initializations == raw.closes == 1


async def test_encrypted_native_data_joins_transaction_and_survives_cold_restart(tmp_path, monkeypatch):
    import os

    from sqlalchemy import select

    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.relational.framework_schema import agentic_memory_table
    from pyfly.data.transaction import TransactionTemplate

    for name in list(os.environ):
        if name.startswith(("PYFLY_DATA_", "PYFLY_AGENTIC_")):
            monkeypatch.delenv(name)
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'encrypted.db'}",
                            "ddl-auto": "create",
                        }
                    },
                    "agentic": {
                        "enabled": True,
                        "memory_backend": "relational",
                        "encryption_enabled": True,
                        "encryption_key": "fixture-key-with-exactly-32-bytes!",
                        "plugin_auto_discover": False,
                        "observability_enabled": False,
                    },
                }
            }
        )
    )
    await context.start()
    try:
        memory = context.get_bean(MemoryManager)
        source = context.get_bean(DataSourceRegistry).primary
        with pytest.raises(RuntimeError, match="rollback fixture"):
            async with TransactionTemplate(source).transaction():
                await memory.async_set_fact("rejected", {"amount": 12})
                assert await memory.async_get_fact("rejected") == {"amount": 12}
                raise RuntimeError("rollback fixture")
        assert await memory.async_get_fact("rejected") is None
        await memory.async_set_fact("accepted", {"amount": [12, False, None]})
        table = agentic_memory_table()
        async with TransactionTemplate(source).transaction(read_only=True) as unit:
            row = (await unit.resource.execute(select(table.c.content).where(table.c.key == "accepted"))).scalar_one()
        assert isinstance(row["content"], str)
        assert "amount" not in row["content"]
    finally:
        await context.stop()
    await context.start()
    try:
        restored = context.get_bean(MemoryManager)
        assert restored is not memory
        assert await restored.async_get_fact("accepted") == {"amount": [12, False, None]}
        assert await restored.async_get_fact("rejected") is None
    finally:
        await context.stop()


@pytest.mark.parametrize("mode", ["async", "sync", "reasoning"])
async def test_memory_failure_runs_middleware_cleanup(mode):
    from pydantic_ai.models.test import TestModel

    from pyfly.agentic import AgenticRuntime

    calls = []
    failure = RuntimeError("fictional memory unavailable")

    class BrokenMemory(MemoryManager):
        async def async_get_message_history(self, conversation_id):
            raise failure

        async def async_add_turn(self, *args, **kwargs):
            raise failure

    class Pattern:
        async def execute(self, agent, prompt, **kwargs):
            return SimpleNamespace(output="answer")

    class Probe:
        async def before_run(self, context):
            calls.append("before")

        async def on_error(self, context, error):
            calls.append(error)

    runtime = AgenticRuntime(AgenticConfig(plugin_auto_discover=False), memory=BrokenMemory())
    agent = runtime.agent_factory.create("memory-failure", model=TestModel(), middleware=[Probe()])
    try:
        with pytest.raises(RuntimeError, match="fictional memory unavailable"):
            if mode == "async":
                await agent.run("fixture", conversation_id="one")
            elif mode == "sync":
                await asyncio.to_thread(agent.run_sync, "fixture", conversation_id="one")
            else:
                await agent.run_with_reasoning(Pattern(), "fixture", conversation_id="one")
        assert calls == ["before", failure]
    finally:
        await runtime.aclose()
