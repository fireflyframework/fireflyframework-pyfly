# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A bounded, transient live channel; durable replay belongs to the application store."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Generic, TypeVar

from pyfly.web.sse.response import SseEvent, format_sse_event

T = TypeVar("T")


class AsyncSseEmitter(Generic[T]):
    """One consumer, bounded buffered frames, and awaiting producers with backpressure.

    ``close`` drains queued frames; ``aclose`` aborts and releases queued data.
    Neither operation cancels application jobs or provides persistent replay.
    """

    def __init__(self, *, capacity: int = 256, max_event_bytes: int = 1048576) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("SSE capacity must be a positive integer")
        if type(max_event_bytes) is not int or max_event_bytes < 1:
            raise ValueError("SSE maximum event size must be a positive integer")
        self._capacity, self._max_event_bytes = capacity, max_event_bytes
        self._frames: deque[str] = deque()
        self._condition = asyncio.Condition()
        self._closed = False
        self._started = False

    @property
    def buffered(self) -> int:
        return len(self._frames)

    @property
    def capacity(self) -> int:
        return self._capacity

    async def send(
        self,
        data: T | SseEvent[T],
        *,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
        comment: str | None = None,
    ) -> None:
        if isinstance(data, SseEvent):
            if any(value is not None for value in (event, id, retry, comment)):
                raise ValueError("An SSE event already owns its metadata")
            frame = data.encode(max_bytes=self._max_event_bytes)
        else:
            frame = format_sse_event(data, event=event, id=id, retry=retry, comment=comment)
            if len(frame.encode("utf-8")) > self._max_event_bytes:
                raise ValueError("SSE event exceeds the configured size limit")
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or len(self._frames) < self._capacity)
            if self._closed:
                raise RuntimeError("SSE emitter is closed")
            self._frames.append(frame)
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def aclose(self) -> None:
        async with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()

    async def __aenter__(self) -> AsyncSseEmitter[T]:
        if self._closed:
            raise RuntimeError("SSE emitter is closed")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def __aiter__(self) -> AsyncGenerator[str, None]:
        if self._started:
            raise RuntimeError("SSE emitter supports one consumer")
        self._started = True
        try:
            while True:
                async with self._condition:
                    await self._condition.wait_for(lambda: self._frames or self._closed)
                    if not self._frames:
                        return
                    frame = self._frames.popleft()
                    self._condition.notify_all()
                yield frame
        finally:
            await self.aclose()
