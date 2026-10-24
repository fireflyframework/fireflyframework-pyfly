# Copyright 2026 Firefly Software Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Memory manager: single entry point for all memory operations.

:class:`MemoryManager` composes :class:`ConversationMemory` and
:class:`WorkingMemory` behind a unified API.  It is the object that
gets wired into :class:`Agent`, :class:`DelegationRouter`,
pipeline steps, and reasoning patterns.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelMessage

from pyfly.agentic.config import AgenticConfig, get_config
from pyfly.agentic.memory.conversation import ConversationMemory
from pyfly.agentic.memory.store import FileStore, InMemoryStore, MemoryStore
from pyfly.agentic.memory.working import WorkingMemory

if TYPE_CHECKING:
    from pyfly.agentic.security.encryption import EncryptionProvider

logger = logging.getLogger(__name__)


class MemoryManager:
    """Unified memory facade for agents, pipelines, and reasoning patterns.

    A ``MemoryManager`` owns one :class:`ConversationMemory` (for chat
    history) and one :class:`WorkingMemory` (for session facts).  It
    provides convenience methods that combine both.

    Parameters:
        store: Backend used by :class:`WorkingMemory`.  Defaults to
            :class:`InMemoryStore`.
        max_conversation_tokens: Token budget for conversation history.
        summarize_threshold: Turns before summarization kicks in.
        working_scope_id: Scope identifier for working memory.
    """

    def __init__(
        self,
        *,
        store: MemoryStore | None = None,
        max_conversation_tokens: int = 128_000,
        summarize_threshold: int = 10,
        working_scope_id: str = "default",
    ) -> None:
        self._store = store if store is not None else InMemoryStore()
        self._conversation = ConversationMemory(
            max_tokens=max_conversation_tokens,
            summarize_threshold=summarize_threshold,
        )
        self._working = WorkingMemory(
            store=self._store,
            scope_id=working_scope_id,
        )

    @classmethod
    def from_config(
        cls,
        config: AgenticConfig | None = None,
        *,
        initialize: bool = True,
        store: MemoryStore | None = None,
        encryption_provider: EncryptionProvider | None = None,
        owns_store: bool = True,
    ) -> MemoryManager:
        """Compose memory around native persistence resolved by the application.

        Database connections and transaction managers belong to PyFly Data. The
        facade never reconstructs them from a second set of pool settings.
        """
        cfg = config if config is not None else get_config()
        selected: MemoryStore
        if store is not None:
            selected = store
        elif cfg.memory_backend == "file":
            selected = FileStore(base_dir=cfg.memory_file_dir)
        elif cfg.memory_backend == "in_memory":
            selected = InMemoryStore()
        elif cfg.memory_backend in {"relational", "postgres", "mongodb"}:
            raise ValueError(
                "Database memory requires a pyfly.data-backed MemoryStore. Enable Agentic in "
                "ApplicationContext with its native datasource configuration, or supply store= explicitly."
            )
        else:
            raise ValueError(f"Unknown memory_backend: {cfg.memory_backend!r}")
        if cfg.encryption_enabled:
            from pyfly.agentic.security.encryption import (
                EncryptedMemoryStore,
                create_encryption_provider_from_config,
            )

            provider = encryption_provider or create_encryption_provider_from_config(cfg)
            if provider is None:
                raise ValueError("encryption_key or an injected EncryptionProvider is required")
            if not isinstance(selected, EncryptedMemoryStore):
                selected = EncryptedMemoryStore(selected, provider=provider, owns_store=owns_store)
        if initialize:
            initialize_sync = getattr(selected, "initialize_sync", None)
            if initialize_sync is not None:
                initialize_sync()
        return cls(
            store=selected,
            max_conversation_tokens=cfg.memory_max_conversation_tokens,
            summarize_threshold=cfg.memory_summarize_threshold,
        )

    # -- Accessors ---------------------------------------------------------

    @property
    def conversation(self) -> ConversationMemory:
        """The conversation memory subsystem."""
        return self._conversation

    @property
    def working(self) -> WorkingMemory:
        """The working memory subsystem."""
        return self._working

    @property
    def store(self) -> MemoryStore:
        """The underlying persistence backend."""
        return self._store

    # -- Conversation shortcuts --------------------------------------------

    def get_message_history(self, conversation_id: str) -> list[ModelMessage]:
        """Shortcut to :meth:`ConversationMemory.get_message_history`."""
        return self._conversation.get_message_history(conversation_id)

    def add_turn(
        self,
        conversation_id: str,
        user_prompt: str,
        assistant_response: str,
        raw_messages: list[ModelMessage],
        **kwargs: Any,
    ) -> Any:
        """Shortcut to :meth:`ConversationMemory.add_turn`."""
        return self._conversation.add_turn(conversation_id, user_prompt, assistant_response, raw_messages, **kwargs)

    def new_conversation(self) -> str:
        """Create a new conversation and return its ID."""
        return self._conversation.new_conversation()

    # -- Working memory shortcuts ------------------------------------------

    def set_fact(self, key: str, value: Any, **kwargs: Any) -> None:
        """Store a fact in working memory."""
        self._working.set(key, value, **kwargs)

    def get_fact(self, key: str, default: Any = None) -> Any:
        """Retrieve a fact from working memory."""
        return self._working.get(key, default)

    def get_working_context(self) -> str:
        """Render working memory as a text block for prompt injection."""
        return self._working.to_context_string()

    # -- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the shared store; borrowed Data resources remain owned by Data."""
        closer = getattr(self._store, "close_sync", None)
        if closer is None:
            closer = getattr(self._store, "close", None)
        if closer is not None:
            if inspect.iscoroutinefunction(closer):
                raise RuntimeError("Use await memory.aclose() for an asynchronous memory backend")
            closer()

    async def aclose(self) -> None:
        closer = getattr(self._store, "aclose", None) or getattr(self._store, "close", None)
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result

    async def async_set_fact(self, key: str, value: Any, **kwargs: Any) -> None:
        await self._working.async_set(key, value, **kwargs)

    async def async_get_fact(self, key: str, default: Any = None) -> Any:
        return await self._working.async_get(key, default)

    async def async_get_working_context(self) -> str:
        return await self._working.async_to_context_string()

    async def async_get_message_history(self, conversation_id: str) -> list[ModelMessage]:
        return self.get_message_history(conversation_id)

    async def async_add_turn(
        self,
        conversation_id: str,
        user_prompt: str,
        assistant_response: str,
        raw_messages: list[ModelMessage],
        **kwargs: Any,
    ) -> Any:
        return self.add_turn(conversation_id, user_prompt, assistant_response, raw_messages, **kwargs)

    def clear_conversation(self, conversation_id: str) -> None:
        """Clear a single conversation."""
        self._conversation.clear(conversation_id)

    def clear_working(self) -> None:
        """Clear working memory."""
        self._working.clear()

    def clear_all(self) -> None:
        """Clear all memory (conversation + working)."""
        self._conversation.clear_all()
        self._working.clear()

    def fork(self, *, working_scope_id: str) -> MemoryManager:
        """Create a child :class:`MemoryManager` that shares the same
        conversation memory and store but has an independent working
        memory scope.

        This is used when delegating to a sub-agent or forking a
        pipeline branch: the child can read/write its own facts without
        polluting the parent's working memory, while still sharing
        conversation context.
        """
        # Bypass __init__ to avoid creating a new store/conversation.
        # The child shares the parent's store and conversation objects
        # (reference sharing, not copy) but gets a fresh WorkingMemory
        # under its own scope to isolate working facts.
        child = MemoryManager.__new__(MemoryManager)
        child._store = self._store
        child._conversation = self._conversation  # shared reference
        child._working = WorkingMemory(store=self._store, scope_id=working_scope_id)
        return child

    def __repr__(self) -> str:
        return (
            f"MemoryManager(conversations={len(self._conversation.conversation_ids)}, "
            f"working_scope={self._working.scope_id!r})"
        )
