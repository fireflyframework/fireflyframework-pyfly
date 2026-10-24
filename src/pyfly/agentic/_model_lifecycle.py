# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Loop affinity and lifecycle for application-owned provider models."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from pydantic_ai.models import Model

from pyfly.agentic.exceptions import AgentError


class ModelState:
    def __init__(self, model: Model, *, owned: bool = False, rebind_on_restart: bool = False) -> None:
        self.model = model
        self.owned = owned
        self._rebind_on_restart = rebind_on_restart
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._entry_lock: asyncio.Lock | None = None
        self._entered = False
        self._closed = False

    def restart(self) -> bool:
        """Prepare a successfully closed owned model for a new SDK context entry."""
        if not self.owned or not self._closed:
            return False
        with self._lock:
            if (
                not self._rebind_on_restart
                and self.loop is not None
                and (self.loop.is_closed() or not self.loop.is_running())
            ):
                return False
            if self._rebind_on_restart:
                self.loop = None
            self._entry_lock = None
            self._entered = False
            self._closed = False
        return True

    def owner(self) -> asyncio.AbstractEventLoop | None:
        with self._lock:
            loop = self.loop
            if loop is not None and (loop.is_closed() or not loop.is_running()):
                raise AgentError("Provider model's owning event loop is closed or stopped; create a new model/client")
            return loop

    def claim(self) -> asyncio.AbstractEventLoop:
        current = asyncio.get_running_loop()
        with self._lock:
            if self.loop is None:
                self.loop = current
        owner = self.owner()
        assert owner is not None
        return owner

    async def enter(self) -> None:
        if not self.owned or self._entered:
            return
        if self._entry_lock is None:
            self._entry_lock = asyncio.Lock()
        async with self._entry_lock:
            if not self._entered:
                await self.model.__aenter__()
                self._entered = True

    async def _close(self) -> None:
        if self._closed or not self.owned:
            return
        await self.enter()
        await self.model.__aexit__(None, None, None)
        self._closed = True

    async def aclose(self) -> None:
        if self._closed or not self.owned:
            return
        owner = self.owner()
        if owner is None:
            self.claim()
            await self._close()
        elif owner is asyncio.get_running_loop():
            await self._close()
        else:
            future = asyncio.run_coroutine_threadsafe(self._close(), owner)
            await asyncio.wrap_future(future)


async def on_model_loop(state: ModelState | None, operation: Any) -> Any:
    """Execute a complete operation on the provider's first-use loop."""
    if state is None:
        return await operation()
    owner = state.claim()
    if owner is not asyncio.get_running_loop():
        future = asyncio.run_coroutine_threadsafe(on_model_loop(state, operation), owner)
        return await asyncio.wrap_future(future)
    await state.enter()
    return await operation()
