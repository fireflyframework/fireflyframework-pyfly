# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Synchronous access to an already initialized application's memory adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from pyfly.agentic.exceptions import DatabaseStoreError
from pyfly.agentic.memory.types import MemoryEntry

T = TypeVar("T")


class ApplicationLoopStore:
    """Borrow resources on their application's loop; never start another database loop."""

    def __init__(self) -> None:
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop | None:
        """Application loop used by the facade's asynchronous thread bridge."""
        return self._owner_loop

    def _check_loop(self, *, initialize: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if self._owner_loop is None and initialize:
            self._owner_loop = loop
        elif self._owner_loop is not None and self._owner_loop is not loop:
            raise DatabaseStoreError("Memory data resources belong to another application event loop")

    def _sync(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        loop = self._owner_loop
        if loop is None or not loop.is_running():
            raise DatabaseStoreError(
                "Initialize memory with 'await store.initialize()' on the running application loop"
            )
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            raise DatabaseStoreError("Use async memory methods on the application's owning event loop")
        # run_coroutine_threadsafe copies this thread's context, including its ambient unit of work.
        return asyncio.run_coroutine_threadsafe(operation(), loop).result()

    def initialize_sync(self) -> None:
        self._sync(self.initialize)

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        self._sync(lambda: self.async_save(namespace, entry))

    def load(self, namespace: str) -> list[MemoryEntry]:
        return self._sync(lambda: self.async_load(namespace))

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        return self._sync(lambda: self.async_load_by_key(namespace, key))

    def delete(self, namespace: str, entry_id: str) -> None:
        self._sync(lambda: self.async_delete(namespace, entry_id))

    def clear(self, namespace: str) -> None:
        self._sync(lambda: self.async_clear(namespace))

    def replace(self, namespace: str, entry: MemoryEntry) -> None:
        self._sync(lambda: self.async_replace(namespace, entry))

    def close_sync(self) -> None:
        self._sync(self.close)

    async def initialize(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        raise NotImplementedError

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        raise NotImplementedError

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        raise NotImplementedError

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        raise NotImplementedError

    async def async_clear(self, namespace: str) -> None:
        raise NotImplementedError

    async def async_replace(self, namespace: str, entry: MemoryEntry) -> None:
        raise NotImplementedError
