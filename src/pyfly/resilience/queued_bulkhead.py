# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Bounded FIFO admission for asynchronous workers before they claim durable jobs."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pyfly.kernel.exceptions import BulkheadException


class QueuedBulkhead:
    """Limit active work and waiting callers within one process and event loop.

    A zero ``max_waiters`` rejects saturation immediately. Positive values allow
    FIFO waiting for at most ``wait_timeout_seconds``. Acquire before claiming a
    durable job; keep the permit until the work and its cleanup have finished.
    Use ``hold()`` for automatic release, or pair each successful ``acquire()``
    with exactly one ``release()``. Permits may be returned by another task on
    the same loop. ``close()`` rejects pending and new callers; active owners
    remain responsible for completing or cancelling their work and releasing.
    """

    def __init__(self, max_concurrent: int = 10, *, max_waiters: int = 0, wait_timeout_seconds: float = 30.0) -> None:
        if type(max_concurrent) is not int or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        if type(max_waiters) is not int or max_waiters < 0:
            raise ValueError("max_waiters must be a nonnegative integer")
        if (
            isinstance(wait_timeout_seconds, bool)
            or not isinstance(wait_timeout_seconds, (int, float))
            or not math.isfinite(wait_timeout_seconds)
            or wait_timeout_seconds <= 0
        ):
            raise ValueError("wait_timeout_seconds must be finite and positive")
        self._max_concurrent = max_concurrent
        self._max_waiters = max_waiters
        self._wait_timeout_seconds = float(wait_timeout_seconds)
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("QueuedBulkhead belongs to another event loop")
        return loop

    async def acquire(self) -> None:
        """Reserve capacity or raise ``BulkheadException`` on saturation, timeout or closure."""
        loop = self._bind_loop()
        if self._closed:
            raise BulkheadException("Queued bulkhead is closed")
        if not self._waiters and self._active < self._max_concurrent:
            self._active += 1
            return
        if len(self._waiters) >= self._max_waiters:
            raise BulkheadException("Queued bulkhead waiting capacity is exhausted")
        waiter = loop.create_future()
        self._waiters.append(waiter)
        try:
            async with asyncio.timeout(self._wait_timeout_seconds):
                await asyncio.shield(waiter)
        except BaseException as error:
            # Assignment and cancellation may happen before the waiting task resumes.
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                self.release()
            else:
                waiter.cancel()
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            if isinstance(error, TimeoutError):
                raise BulkheadException("Queued bulkhead admission timed out") from None
            raise

    def release(self) -> None:
        """Return one acquired permit and assign it to the oldest waiting caller."""
        self._bind_loop()
        if self._active == 0:
            raise RuntimeError("No queued bulkhead permit is active")
        self._active -= 1
        while not self._closed and self._waiters and self._active < self._max_concurrent:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self._active += 1
                waiter.set_result(None)

    def close(self) -> None:
        """Permanently stop admission without cancelling active work."""
        if self._loop is not None:
            self._bind_loop()
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(BulkheadException("Queued bulkhead is closed"))

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Hold a permit through successful, failed or cancelled work and its cleanup."""
        await self.acquire()
        try:
            yield
        finally:
            self.release()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_waiters(self) -> int:
        return self._max_waiters

    @property
    def wait_timeout_seconds(self) -> float:
        return self._wait_timeout_seconds

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def available_slots(self) -> int:
        return self._max_concurrent - self._active

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    @property
    def closed(self) -> bool:
        return self._closed
