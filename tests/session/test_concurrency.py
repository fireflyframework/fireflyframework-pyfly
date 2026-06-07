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
"""Session concurrency control (v26.06.55) — max-sessions-per-principal."""

from __future__ import annotations

import pytest

from pyfly.session.concurrency import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
)


@pytest.mark.asyncio
async def test_registry_tracks_sessions_oldest_first() -> None:
    reg = InMemorySessionRegistry()
    await reg.register("alice", "s1", 1.0)
    await reg.register("alice", "s2", 2.0)
    await reg.register("bob", "s3", 5.0)

    assert await reg.count("alice") == 2
    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s1", "s2"]  # oldest first

    await reg.deregister("alice", "s1")
    assert await reg.count("alice") == 1
    await reg.deregister("bob", "s3")
    assert await reg.count("bob") == 0  # principal bucket pruned


@pytest.mark.asyncio
async def test_unlimited_always_allows() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=-1))
    for i in range(5):
        assert await ctl.on_login("alice", f"s{i}", float(i)) is True
    assert await reg.count("alice") == 5


@pytest.mark.asyncio
async def test_reject_new_strategy() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=2, strategy="reject-new"))
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is True
    assert await ctl.on_login("alice", "s3", 3.0) is False  # over cap -> rejected
    assert {sid for sid, _ in await reg.list_sessions("alice")} == {"s1", "s2"}  # s3 not registered


@pytest.mark.asyncio
async def test_evict_oldest_strategy_deletes_evicted_session() -> None:
    reg = InMemorySessionRegistry()
    deleted: list[str] = []

    async def _delete(session_id: str) -> None:
        deleted.append(session_id)

    ctl = SessionConcurrencyController(
        reg, ConcurrencyControlPolicy(max_sessions=2, strategy="evict-oldest"), session_deleter=_delete
    )
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is True
    assert await ctl.on_login("alice", "s3", 3.0) is True  # evicts oldest (s1), allows s3

    assert deleted == ["s1"]  # oldest session purged from the store
    assert {sid for sid, _ in await reg.list_sessions("alice")} == {"s2", "s3"}
    assert await reg.count("alice") == 2  # cap held


@pytest.mark.asyncio
async def test_on_logout_deregisters() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=5))
    await ctl.on_login("alice", "s1", 1.0)
    await ctl.on_logout("alice", "s1")
    assert await reg.count("alice") == 0
