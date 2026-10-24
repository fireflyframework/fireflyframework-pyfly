"""Await native store operations, or offload a synchronous custom backend."""

from __future__ import annotations

import asyncio
from typing import Any

from pyfly.agentic.exceptions import DatabaseStoreError


async def call_store(store: object, operation: str, *args: Any) -> Any:
    async def invoke() -> Any:
        asynchronous = getattr(store, f"async_{operation}", None)
        if asynchronous is not None:
            return await asynchronous(*args)
        return await asyncio.to_thread(getattr(store, operation), *args)

    owner = getattr(store, "owner_loop", None)
    if owner is not None and owner is not asyncio.get_running_loop():
        if owner.is_closed() or not owner.is_running():
            raise DatabaseStoreError("Memory's application event loop is not running")
        from pyfly.agentic._runtime_context import selected_runtime

        runtime = selected_runtime()
        if runtime is not None and runtime._application_loop is owner:
            return await runtime.on_application_loop(invoke)
        future = asyncio.run_coroutine_threadsafe(invoke(), owner)
        return await asyncio.wrap_future(future)
    return await invoke()
