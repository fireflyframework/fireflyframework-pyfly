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
"""The session cap through the OAuth2 login flow (WP10b: C076, C155).

The controller counts a session while the session store has it. The login handler saves the session it
logs in before registering it, so a concurrent login of the same principal does not take a session whose
response has not been sent yet (and so not persisted by the SessionFilter) for a dead one.

That save is the login's last write of the session: with evict-oldest, the filter's final persist saved the
session again when the handler returned, so a session a concurrent login had evicted meanwhile came back, live
and no longer counted (8 concurrent logins under a cap of one left 8 logged-in sessions). The scenario is
shared with the SQL and Redis integration tests (``tests/integration/_session_logins.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.concurrency import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
)
from tests.integration import _session_logins as logins


class _YieldingStore(InMemorySessionStore):
    """The in-memory store, yielding to the event loop on every call as a store over a network does."""

    async def get(self, session_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return await super().get(session_id)

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        await asyncio.sleep(0)
        await super().save(session_id, data, ttl)

    async def delete(self, session_id: str) -> None:
        await asyncio.sleep(0)
        await super().delete(session_id)

    async def exists(self, session_id: str) -> bool:
        await asyncio.sleep(0)
        return await super().exists(session_id)


def _controller(store: InMemorySessionStore, *, max_sessions: int, strategy: str) -> SessionConcurrencyController:
    return SessionConcurrencyController(
        InMemorySessionRegistry(),
        ConcurrencyControlPolicy(max_sessions=max_sessions, strategy=strategy),
        session_store=store,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [2, 8])
async def test_max_sessions_one_admits_one_of_concurrent_oauth2_logins(concurrent: int) -> None:
    store = _YieldingStore()
    replica = logins.Replica(store, _controller(store, max_sessions=1, strategy="reject-new"))
    pre_auth = [await logins.start_login(store) for _ in range(concurrent)]

    results = await asyncio.gather(*(logins.login(replica, session_id) for session_id in pre_auth))

    statuses = [result.status for result in results]
    assert statuses.count(302) == 1, statuses
    assert statuses.count(401) == concurrent - 1
    assert replica.controller is not None
    assert await replica.controller.registry.count("alice") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["evict-oldest", "reject-new"])
@pytest.mark.parametrize(("max_sessions", "concurrent"), [(1, 8), (2, 16)])
async def test_concurrent_logins_keep_the_cap(strategy: str, max_sessions: int, concurrent: int) -> None:
    """Rounds of concurrent logins of one principal, over two instances sharing the store and the registry:
    after each round only the registered sessions are in the store, and an evicted session stays evicted."""
    store = _YieldingStore()
    controller = _controller(store, max_sessions=max_sessions, strategy=strategy)
    replicas = [logins.Replica(store, controller), logins.Replica(store, controller)]

    await logins.concurrent_logins_keep_the_cap(
        replicas,
        max_sessions=max_sessions,
        evict_oldest=strategy == "evict-oldest",
        concurrent=concurrent,
        rounds=5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["evict-oldest", "reject-new"])
async def test_one_login_at_a_time_keeps_the_latest_or_the_first_sessions(strategy: str) -> None:
    store = _YieldingStore()
    replicas = [logins.Replica(store, _controller(store, max_sessions=2, strategy=strategy))]

    await logins.concurrent_logins_keep_the_cap(
        replicas, max_sessions=2, evict_oldest=strategy == "evict-oldest", concurrent=1, rounds=4
    )


@pytest.mark.asyncio
async def test_a_login_without_a_cap_saves_the_logged_in_session() -> None:
    store = _YieldingStore()
    replica = logins.Replica(store, None)

    result = await logins.login(replica, await logins.start_login(store))

    assert (result.status, result.location) == (302, logins.AFTER_LOGIN)
    saved = await store.get(result.session_id)
    assert saved is not None and saved["SECURITY_CONTEXT"].user_id == "alice"
    assert "oauth2_state" not in saved and "oauth2_redirect_uri" not in saved


@pytest.mark.asyncio
async def test_a_login_after_the_sessions_expired_is_admitted() -> None:
    """C076 end to end: the user closed the browser (no logout) and the session expired."""
    store = _YieldingStore()
    replica = logins.Replica(store, _controller(store, max_sessions=1, strategy="reject-new"))
    assert replica.controller is not None
    first = await logins.login(replica, await logins.start_login(store))
    assert first.status == 302
    assert [sid for sid, _ in await replica.controller.registry.list_sessions("alice")] == [first.session_id]
    await store.save(first.session_id, {}, ttl=-1)  # expired

    second = await logins.login(replica, await logins.start_login(store))

    assert second.status == 302
    assert [sid for sid, _ in await replica.controller.registry.list_sessions("alice")] == [second.session_id]
