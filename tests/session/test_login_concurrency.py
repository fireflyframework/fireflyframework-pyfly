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
import logging
from typing import Any

import pytest
from starlette.responses import Response

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.concurrency import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
    SessionRegistration,
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

    async def replace(self, session_id: str, data: dict[str, Any], ttl: int) -> bool:
        await asyncio.sleep(0)
        return await super().replace(session_id, data, ttl)


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
@pytest.mark.parametrize(("max_sessions", "concurrent"), [(1, 8), (2, 16)])
async def test_a_session_write_after_the_login_keeps_the_cap(strategy: str, max_sessions: int, concurrent: int) -> None:
    """The application changes the session after the login handler returned: the filter's final persist must
    not bring back a session a concurrent login evicted meanwhile."""
    store = _YieldingStore()
    controller = _controller(store, max_sessions=max_sessions, strategy=strategy)
    replicas = [logins.Replica(store, controller, write_after_login=True) for _ in range(2)]

    await logins.concurrent_logins_keep_the_cap(
        replicas,
        max_sessions=max_sessions,
        evict_oldest=strategy == "evict-oldest",
        concurrent=concurrent,
        rounds=5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["eviction", "logout", "logout-filter"])
async def test_a_revoked_session_stays_revoked(revocation: str) -> None:
    store = _YieldingStore()
    replica = logins.Replica(store, _controller(store, max_sessions=1, strategy="evict-oldest"))

    await logins.a_revoked_session_stays_revoked(replica, revocation)


@pytest.mark.asyncio
async def test_a_request_ending_after_another_tab_logged_in_leaves_the_new_cookie_alone() -> None:
    """Tab A's request of the pre-authentication session S1 changes it and is still running when tab B, in the
    same browser, logs in: the login rotates S1 to S2 and sets S2's cookie. Tab A's request then finds S1 gone.
    Its response cleared the session cookie, and when it arrived last the browser lost S2's."""
    store = _YieldingStore()
    replica = logins.Replica(store, _controller(store, max_sessions=-1, strategy="evict-oldest"))
    s1 = await logins.start_login(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def in_flight(request: Any) -> Any:
        request.state.session.set_attribute("cart", ["book"])
        entered.set()
        await release.wait()
        return Response()

    tab_a = asyncio.create_task(replica.session_filter.do_filter(logins.app_request(s1), in_flight))
    await asyncio.wait_for(entered.wait(), 10)
    tab_b = await logins.login(replica, s1)
    release.set()
    tab_a_response = await asyncio.wait_for(tab_a, 10)

    assert tab_b.status == 302 and await store.exists(tab_b.session_id)
    assert not await store.exists(s1)
    assert logins.session_cookies(tab_a_response) == []


class _RegistryDown(InMemorySessionRegistry):
    """The in-memory registry, whose deregistrations fail once :attr:`down` is set (a registry outage)."""

    down = False

    async def deregister(self, principal: str, session_id: str) -> None:
        if self.down:
            raise ConnectionError("the session registry is unreachable")
        await super().deregister(principal, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["logout", "logout-filter"])
async def test_a_logout_survives_a_registry_outage(via: str, caplog: pytest.LogCaptureFixture) -> None:
    store = _YieldingStore()
    registry = _RegistryDown()
    controller = SessionConcurrencyController(registry, ConcurrencyControlPolicy(max_sessions=1), session_store=store)

    async def break_registry() -> None:
        registry.down = True

    with caplog.at_level(logging.WARNING):
        await logins.a_logout_survives_a_registry_outage(logins.Replica(store, controller), via, break_registry)

    assert "session_deregistration_failed" in caplog.messages


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


class _UnreachableRegistry(InMemorySessionRegistry):
    """A registry whose database does not answer (the SQL registry after its three reruns, or down)."""

    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration:
        await asyncio.sleep(0)
        raise ConnectionError("the session registry is unreachable")


@pytest.mark.asyncio
async def test_a_login_whose_registration_fails_leaves_no_session_behind() -> None:
    """The handler saves the session before registering it: when the registration fails, that session must
    not stay in the store, logged in and never counted by the cap."""
    store = _YieldingStore()
    controller = SessionConcurrencyController(
        _UnreachableRegistry(), ConcurrencyControlPolicy(max_sessions=1), session_store=store
    )
    replica = logins.Replica(store, controller)
    pre_auth = await logins.start_login(store)
    request = logins.callback_request(pre_auth)

    with pytest.raises(ConnectionError):
        await logins.run_callback(replica, request)

    session = request.state.session
    assert session.id != pre_auth and session.invalidated
    assert not await store.exists(session.id)
    assert not await store.exists(pre_auth)


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
