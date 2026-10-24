# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Lazy persistent event loop owned by one application runtime."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class WorkerLoop:
    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

    def get(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                self.thread = threading.Thread(target=self._serve, args=(self.loop,), name=self.name, daemon=True)
                self.thread.start()
            return self.loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.wait(pending, timeout=5))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def run(self, coro: Any) -> Any:
        loop = self.get()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            coro.close()
            raise RuntimeError("Cannot block an Agentic worker on its own event loop; use the async API")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    def close(self) -> None:
        loop, thread = self.loop, self.thread
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=6)
            if thread.is_alive():
                raise RuntimeError(f"Agentic worker {self.name} did not stop")
        self.loop = None
        self.thread = None
