"""Cancellation of a cache follower cannot cancel shared work."""

import asyncio

import pytest

from pyfly.agentic.tools.cached import CachedTool


async def test_cancelled_follower_leaves_leader_and_other_follower_alive():
    started, release, followers = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Tool:
        name = "controlled"
        description = "controlled"
        calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return "result"

    tool = Tool()
    cached = CachedTool(tool)
    leader = asyncio.create_task(cached.execute())
    await started.wait()
    cancelled = asyncio.create_task(cached.execute())
    survivor = asyncio.create_task(cached.execute())
    asyncio.get_running_loop().call_soon(followers.set)
    await followers.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    results = await asyncio.gather(leader, survivor, return_exceptions=True)
    assert results == ["result", "result"]
    assert await cached.execute() == "result"
    assert tool.calls == 1
    assert not cached._inflight


async def test_leader_cancelled_during_cache_write_releases_followers_and_inflight():
    started, release, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Tool:
        name = "controlled"
        description = "controlled"

        async def execute(self, **kwargs):
            started.set()
            await release.wait()
            return "result"

    cached = CachedTool(Tool())
    leader = asyncio.create_task(cached.execute())
    await started.wait()
    follower = asyncio.create_task(cached.execute())
    entered = asyncio.Event()
    asyncio.get_running_loop().call_soon(entered.set)
    await entered.wait()
    await cached._lock.acquire()
    release.set()
    asyncio.get_running_loop().call_soon(writing.set)
    await writing.wait()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    cached._lock.release()
    watchdog = asyncio.timeout(0.5)
    try:
        async with watchdog:
            with pytest.raises(asyncio.CancelledError):
                await follower
    finally:
        if not follower.done():
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
    assert not watchdog.expired()
    assert not cached._inflight
    assert await cached.execute() == "result"
