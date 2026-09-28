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
"""InProcessDistributedLock honors the DistributedLock contract: a lock ends at its ttl, and a holder whose
lock ended does not release the lock another holder took since."""

from __future__ import annotations

import asyncio

from pyfly.scheduling.lock import DistributedLock, InProcessDistributedLock


async def test_a_late_release_does_not_end_the_lock_another_holder_took_since() -> None:
    lock = InProcessDistributedLock()
    assert isinstance(lock, DistributedLock)
    took_over = asyncio.Event()
    late_release_done = asyncio.Event()

    async def hung_job() -> None:
        assert await lock.try_acquire("nightly", 0.05) is True
        await took_over.wait()  # it ran past its ttl, and another run took the lock meanwhile
        await lock.release("nightly")
        late_release_done.set()

    async def next_run() -> bool:
        await asyncio.sleep(0.1)
        assert await lock.try_acquire("nightly", 30.0) is True
        took_over.set()
        await late_release_done.wait()
        still_held = not await asyncio.create_task(lock.try_acquire("nightly", 30.0))
        await lock.release("nightly")
        return still_held

    _, still_held = await asyncio.gather(hung_job(), next_run())

    assert still_held is True
    assert await lock.try_acquire("nightly", 30.0) is True  # the holder's own release freed it


async def test_the_holder_may_release_from_another_task() -> None:
    lock = InProcessDistributedLock()
    assert await asyncio.create_task(lock.try_acquire("nightly", 30.0)) is True

    await lock.release("nightly")

    assert await lock.try_acquire("nightly", 30.0) is True


async def test_a_lock_ends_at_its_ttl() -> None:
    lock = InProcessDistributedLock()
    assert await lock.try_acquire("nightly", 0.05) is True
    assert await lock.try_acquire("nightly", 30.0) is False
    await asyncio.sleep(0.1)
    assert await lock.try_acquire("nightly", 30.0) is True
