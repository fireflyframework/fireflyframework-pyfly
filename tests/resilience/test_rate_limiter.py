# Copyright 2026 Firefly Software Foundation.
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
"""Tests for rate limiter using token bucket algorithm."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.kernel.exceptions import RateLimitException
from pyfly.resilience.rate_limiter import RateLimiter, rate_limiter


@pytest.mark.asyncio
async def test_acquire_within_capacity() -> None:
    """Can acquire up to max_tokens without exception."""
    limiter = RateLimiter(max_tokens=5, refill_rate=0.0)

    for _ in range(5):
        await limiter.acquire()


@pytest.mark.asyncio
async def test_acquire_exceeds_capacity() -> None:
    """Raises RateLimitException when tokens exhausted."""
    limiter = RateLimiter(max_tokens=2, refill_rate=0.0)

    await limiter.acquire()
    await limiter.acquire()

    with pytest.raises(RateLimitException):
        await limiter.acquire()


@pytest.mark.asyncio
async def test_tokens_refill_over_time() -> None:
    """After exhausting tokens, waiting allows new acquisitions."""
    limiter = RateLimiter(max_tokens=2, refill_rate=20.0)

    # Exhaust all tokens.
    await limiter.acquire()
    await limiter.acquire()

    with pytest.raises(RateLimitException):
        await limiter.acquire()

    # Wait for refill (20 tokens/sec -> 0.15s should give ~3 tokens, capped at 2).
    await asyncio.sleep(0.15)

    # Should succeed after refill.
    await limiter.acquire()


@pytest.mark.asyncio
async def test_decorator_allows_within_limit() -> None:
    """Decorated function succeeds within rate limit."""
    limiter = RateLimiter(max_tokens=3, refill_rate=0.0)

    @rate_limiter(limiter)
    async def my_func(x: int) -> int:
        return x * 2

    assert await my_func(5) == 10
    assert await my_func(7) == 14
    assert await my_func(3) == 6


@pytest.mark.asyncio
async def test_decorator_rejects_over_limit() -> None:
    """Decorated function raises RateLimitException when limit exceeded."""
    limiter = RateLimiter(max_tokens=2, refill_rate=0.0)

    @rate_limiter(limiter)
    async def my_func() -> str:
        return "ok"

    await my_func()
    await my_func()

    with pytest.raises(RateLimitException):
        await my_func()


@pytest.mark.asyncio
async def test_available_tokens_property() -> None:
    """Reflects current token count."""
    limiter = RateLimiter(max_tokens=5, refill_rate=0.0)

    assert limiter.available_tokens == pytest.approx(5.0)

    await limiter.acquire()
    assert limiter.available_tokens == pytest.approx(4.0)

    await limiter.acquire()
    assert limiter.available_tokens == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_burst_and_refill() -> None:
    """Exhaust all tokens, wait partial refill, acquire again."""
    limiter = RateLimiter(max_tokens=2, refill_rate=20.0)

    # Exhaust all tokens.
    await limiter.acquire()
    await limiter.acquire()

    # No tokens left.
    with pytest.raises(RateLimitException):
        await limiter.acquire()

    # Wait for partial refill (20 tokens/sec * 0.15s = 3 tokens, capped at 2).
    await asyncio.sleep(0.15)

    # Should be able to acquire again after refill.
    await limiter.acquire()

    # And one more since bucket should have refilled to capacity.
    await limiter.acquire()

    # But a third should fail (only 2 max).
    with pytest.raises(RateLimitException):
        await limiter.acquire()


# ---------------------------------------------------------------------------
# Sync-path thread safety (audit follow-up: sync_wrapper bypassed the lock)
# ---------------------------------------------------------------------------


def test_lock_is_thread_safe_not_asyncio() -> None:
    """The bucket lock must work across OS threads, not just within an event loop."""
    limiter = RateLimiter(max_tokens=3, refill_rate=0.0)
    assert not isinstance(limiter._lock, asyncio.Lock)


def test_concurrent_sync_calls_do_not_overspend_tokens() -> None:
    """Concurrent threaded calls through the sync decorator must not over-consume.

    With ``max_tokens=5`` and no refill, exactly 5 of N concurrent threaded calls
    may succeed — never more. The old ``sync_wrapper`` mutated the token bucket
    without any lock, so racing threads could decrement past zero.
    """
    import threading

    limiter = RateLimiter(max_tokens=5, refill_rate=0.0)

    @rate_limiter(limiter)
    def op() -> str:
        return "ok"

    successes = 0
    count_lock = threading.Lock()
    start = threading.Barrier(64)

    def run() -> None:
        nonlocal successes
        start.wait()  # maximize contention — all threads hit the bucket together
        try:
            op()
        except RateLimitException:
            return
        with count_lock:
            successes += 1

    threads = [threading.Thread(target=run) for _ in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert successes == 5
