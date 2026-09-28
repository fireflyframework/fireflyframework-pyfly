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
"""The OAuth2 login flow as a browser drives it, for the session-cap scenarios (WP10b: C155).

A login here is the provider's callback request through the real ``SessionFilter`` and ``OAuth2LoginHandler``;
only the identity provider's token and userinfo calls are answered locally. The whole request matters: the
handler saves the session before registering it, and the filter persists the session again when the handler
returns, so a test that calls ``store.save`` and ``controller.on_login`` itself never sees a session that its
own request's final persist brought back after a concurrent login evicted it.

:func:`concurrent_logins_keep_the_cap` and :func:`a_revoked_session_stays_revoked` are the scenarios, run by the
unit tests on the in-memory store and by the integration tests on every SQL lane and on Redis. The second one is
about any request, not a login: a request of a session that changes it and ends after the session was revoked
(evicted by a login elsewhere, or logged out) saved it back, and the revoked session authenticated again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response

from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.filter import SessionFilter
from pyfly.session.ports.outbound import SessionStore
from pyfly.web.adapters.starlette.filters.logout_filter import LogoutFilter

#: Where the application sends the browser after a login (saved in the pre-authentication session).
AFTER_LOGIN = "/home"

_REGISTRATION = ClientRegistration(
    registration_id="idp",
    client_id="app",
    client_secret="app-secret",
    redirect_uri="https://app/cb",
    scopes=["openid"],
    authorization_uri="https://idp/auth",
    token_uri="https://idp/token",
    use_pkce=False,
)


class IdpStubbedHandler(OAuth2LoginHandler):
    """The real login handler, with the identity provider's token and userinfo calls answered locally: every
    login authenticates *principal*. :attr:`during_exchange`, when set, runs while the code is exchanged."""

    def __init__(self, principal: str, controller: SessionConcurrencyController | None) -> None:
        super().__init__(InMemoryClientRegistrationRepository(_REGISTRATION), concurrency=controller)
        self._principal = principal
        self.during_exchange: Callable[[], Awaitable[None]] | None = None

    async def _exchange_code(self, registration: Any, code: str, code_verifier: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        if self.during_exchange is not None:
            await self.during_exchange()
        return {"access_token": f"at-{code}"}

    async def _fetch_user_info(self, registration: Any, access_token: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"sub": self._principal}


class Replica:
    """One application instance: its session filter and login handler over *store* and *controller* (which
    several replicas share when their store and registry are shared).

    With *write_after_login*, the application changes the session after the login handler returned (an
    application filter recording the login, say), before the ``SessionFilter`` persists it.
    """

    def __init__(
        self,
        store: SessionStore,
        controller: SessionConcurrencyController | None,
        *,
        principal: str = "alice",
        write_after_login: bool = False,
    ) -> None:
        self.store = store
        self.controller = controller
        self.principal = principal
        self.write_after_login = write_after_login
        self.session_filter = SessionFilter(store=store)
        self.handler = IdpStubbedHandler(principal, controller)
        self.logout_filter = LogoutFilter(concurrency=controller)


@dataclass(frozen=True)
class Login:
    """How a login's request ended: its status, the id the login rotated the session to (the cookie an
    admitted login sends), and where it redirects."""

    status: int
    session_id: str
    location: str | None


async def start_login(store: SessionStore) -> str:
    """Save a pre-authentication session as the authorization redirect leaves it; return its id."""
    session_id = f"pre-{uuid.uuid4().hex}"
    await store.save(session_id, {"oauth2_state": f"state-{session_id}", "oauth2_redirect_uri": AFTER_LOGIN}, ttl=600)
    return session_id


def callback_request(pre_auth_session: str) -> Request:
    """The provider's callback, as the browser holding *pre_auth_session* sends it."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/login/oauth2/code/idp",
            "headers": [(b"cookie", f"PYFLY_SESSION={pre_auth_session}".encode())],
            "query_string": f"state=state-{pre_auth_session}&code=code-{pre_auth_session}".encode(),
            "path_params": {"registration_id": "idp"},
        }
    )


async def run_callback(replica: Replica, request: Request) -> Response:
    """*request* through *replica*'s session filter and login handler, as the application serves it."""

    async def call_next(inner: Request) -> Response:
        response = await replica.handler._handle_callback(inner)
        if replica.write_after_login and response.status_code == 302:
            await asyncio.sleep(0)
            inner.state.session.set_attribute("login_recorded", True)
        return response

    response: Response = await replica.session_filter.do_filter(request, call_next)
    return response


async def login(replica: Replica, pre_auth_session: str) -> Login:
    """The login of the browser holding *pre_auth_session*, served by *replica*."""
    request = callback_request(pre_auth_session)
    response = await run_callback(replica, request)
    return Login(int(response.status_code), request.state.session.id, response.headers.get("location"))


async def concurrent_logins_keep_the_cap(
    replicas: Sequence[Replica], *, max_sessions: int, evict_oldest: bool, concurrent: int, rounds: int
) -> None:
    """*rounds* times, *concurrent* logins of one principal at once, spread over *replicas* (which share one
    store and one registry). After each round the principal's sessions still in the store are exactly the
    registered ones, never more than *max_sessions*: a session a login evicted or rejected, in this round or an
    earlier one, stays gone, and no pre-authentication session is left behind. With one login per round, the
    sessions kept are the latest (evict-oldest) or the first (reject-new)."""
    first = replicas[0]
    assert first.controller is not None
    logged_in: list[str] = []
    for round_number in range(1, rounds + 1):
        pre_auth = [await start_login(first.store) for _ in range(concurrent)]

        logins = await asyncio.gather(
            *(login(replicas[index % len(replicas)], session_id) for index, session_id in enumerate(pre_auth))
        )

        statuses = [each.status for each in logins]
        admitted = [each.session_id for each in logins if each.status == 302]
        if evict_oldest:
            assert statuses == [302] * concurrent, statuses
        else:
            assert len(admitted) == max(0, min(concurrent, max_sessions - len(logged_in))), statuses
            assert statuses.count(401) == concurrent - len(admitted), statuses
        assert {each.location for each in logins if each.status == 302} <= {AFTER_LOGIN}
        logged_in += [each.session_id for each in logins]

        registered = sorted(
            session_id for session_id, _ in await first.controller.registry.list_sessions(first.principal)
        )
        live = sorted([session_id for session_id in logged_in if await first.store.exists(session_id)])
        assert len(registered) == min(max_sessions, len(logged_in))
        assert live == registered, (
            f"round {round_number}: {len(live)} logged-in sessions in the store, {len(registered)} registered, "
            f"cap {max_sessions}"
        )
        if concurrent == 1:
            # One login at a time: evict-oldest keeps the latest sessions, reject-new the first ones.
            kept = logged_in[-max_sessions:] if evict_oldest else logged_in[:max_sessions]
            assert registered == sorted(kept)
        elif evict_oldest and concurrent >= max_sessions:
            # Every session of an earlier round is older than this round's: this round evicted all of them.
            assert set(registered) <= set(admitted)
        assert [session_id for session_id in pre_auth if await first.store.exists(session_id)] == []


# ---------------------------------------------------------------------------------------------------------
# Any request of a session revoked meanwhile
# ---------------------------------------------------------------------------------------------------------


def app_request(session_id: str, path: str = "/cart", method: str = "POST") -> Request:
    """A request of the browser holding the session cookie *session_id*."""
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"cookie", f"PYFLY_SESSION={session_id}".encode())],
            "query_string": b"",
            "path_params": {},
        }
    )


def session_cookies(response: Response) -> list[str]:
    """The session-cookie values *response* sets (an empty value clears the cookie)."""
    values = []
    for name, value in response.raw_headers:
        if name == b"set-cookie" and value.startswith(b"PYFLY_SESSION="):
            values.append(value.split(b";", 1)[0].split(b"=", 1)[1].decode().strip('"'))
    return values


async def authenticated_as(replica: Replica, session_id: str) -> str | None:
    """Who a request of the browser holding *session_id* is authenticated as (``None``: anonymous), through the
    session filter and the filter that restores the security context from the session."""
    security = OAuth2SessionSecurityFilter()
    seen: list[str | None] = []

    async def handler(request: Request) -> Response:
        context: SecurityContext = request.state.security_context
        seen.append(context.user_id if context.is_authenticated else None)
        return Response(status_code=200)

    async def call_next(request: Request) -> Response:
        response: Response = await security.do_filter(request, handler)
        return response

    await replica.session_filter.do_filter(app_request(session_id, "/me", "GET"), call_next)
    return seen[0]


async def a_revoked_session_stays_revoked(replica: Replica, revocation: str) -> None:
    """A request R of a logged-in session S1 changes the session and is still running when S1 is revoked:
    evicted by a login of the same principal on another browser (*revocation* ``eviction``, under a cap of one
    with evict-oldest), or logged out by S1's own request, through the OAuth2 login handler's logout
    (``logout``) or the generic logout filter (``logout-filter``). When R ends, S1 must stay revoked: not in the
    store, not registered with the concurrency controller, and a later request with S1's cookie anonymous. R's
    response sets no session cookie at all: not S1's again, and no deletion either, which could clear a cookie
    another request of the same browser set meanwhile."""
    controller = replica.controller
    assert controller is not None
    first = await login(replica, await start_login(replica.store))
    assert first.status == 302
    s1 = first.session_id
    assert await authenticated_as(replica, s1) == replica.principal

    entered, release = asyncio.Event(), asyncio.Event()

    async def in_flight(request: Request) -> Response:
        request.state.session.set_attribute("cart", ["book"])  # any change of the session
        entered.set()
        await release.wait()
        return Response(status_code=200)

    r_task = asyncio.create_task(replica.session_filter.do_filter(app_request(s1), in_flight))
    await asyncio.wait_for(entered.wait(), 10)

    if revocation == "eviction":
        second = await login(replica, await start_login(replica.store))
        assert second.status == 302
    else:
        assert (await logout(replica, s1, revocation)).status_code == 302
    assert not await replica.store.exists(s1)

    release.set()
    r_response: Response = await asyncio.wait_for(r_task, 10)

    assert not await replica.store.exists(s1), f"{revocation}: the revoked session is back in the store"
    registered = [session_id for session_id, _ in await controller.registry.list_sessions(replica.principal)]
    assert s1 not in registered, f"{revocation}: the revoked session is still registered"
    assert await authenticated_as(replica, s1) is None, f"{revocation}: the revoked session authenticates"
    assert session_cookies(r_response) == [], f"{revocation}: the response sets the session cookie"


async def logout(replica: Replica, session_id: str, via: str) -> Response:
    """The logout request of the browser holding *session_id*, through *replica*'s session filter and the OAuth2
    login handler's logout (*via* ``logout``) or the generic logout filter (``logout-filter``)."""

    async def oauth2_logout(request: Request) -> Response:
        return await replica.handler._handle_logout(request)

    async def logout_filter(request: Request) -> Response:
        response: Response = await replica.logout_filter.do_filter(request, _past_the_logout)
        return response

    handler = oauth2_logout if via == "logout" else logout_filter
    response: Response = await replica.session_filter.do_filter(app_request(session_id, "/logout"), handler)
    return response


async def _past_the_logout(request: Request) -> Response:
    """What the logout filter calls next, which a logout request never reaches."""
    raise AssertionError("the logout filter passed a logout request on")


async def a_logout_survives_a_registry_outage(
    replica: Replica, via: str, break_registry: Callable[[], Awaitable[None]]
) -> None:
    """The session registry fails (a pool timeout, a database or Redis outage) while the session store answers.
    A logout through the OAuth2 login handler (*via* ``logout``) or the generic logout filter (``logout-filter``)
    still ends the session: it answers 302, the session is deleted, and a later request with its cookie is
    anonymous. The logout deregistered the session first and answered 500 with the session left logged in; the
    registration left behind is now dropped by the controller as dead."""
    first = await login(replica, await start_login(replica.store))
    assert first.status == 302
    s1 = first.session_id
    assert await authenticated_as(replica, s1) == replica.principal
    await break_registry()

    response = await logout(replica, s1, via)

    assert response.status_code == 302, f"{via}: the logout failed"
    assert not await replica.store.exists(s1), f"{via}: the logged-out session is still in the store"
    assert await authenticated_as(replica, s1) is None, f"{via}: the logged-out session still authenticates"


async def _gone(store: SessionStore, session_id: str, *, within: float = 5.0) -> bool:
    """Whether the store stops holding *session_id* within *within* seconds (a deletion running in a task of its
    own may finish just after the request that started it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while await store.exists(session_id):
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def a_cancelled_logout_still_ends_the_session(replica: Replica, via: str, registry: Any) -> None:
    """The logout request is cancelled while it deregisters the session (the client went away). A
    level-triggered cancel scope, as anyio's (Starlette's), cancels every later await of the request too, the
    session filter's cleanup included: the deletion of the logged-out session was cancelled with it, and the
    session stayed live. It must end all the same: gone from the store, and a later request with its cookie
    anonymous."""
    first = await login(replica, await start_login(replica.store))
    assert first.status == 302
    s1 = first.session_id
    entered = asyncio.Event()

    async def hanging(principal: str, session_id: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    original = registry.deregister
    registry.deregister = hanging
    try:
        async with anyio.create_task_group() as group:

            async def cancel_once_deregistering() -> None:
                await entered.wait()
                group.cancel_scope.cancel()

            group.start_soon(cancel_once_deregistering)
            group.start_soon(logout, replica, s1, via)
    finally:
        registry.deregister = original

    assert await _gone(replica.store, s1), f"{via}: the logged-out session is still in the store"
    assert await authenticated_as(replica, s1) is None, f"{via}: the logged-out session still authenticates"


async def a_rotation_after_a_revocation_brings_nothing_back(replica: Replica, revocation: str) -> None:
    """A request R of a logged-in session S1 is running when S1 is revoked (*revocation* as for
    :func:`a_revoked_session_stays_revoked`); R then rotates the session id (a privilege elevation, not a login)
    and changes the session. The rotation inserted the session under its new id S2, sent S2's cookie, and S2
    authenticated: the revoked session was back under another id. Now neither id is in the store, R's response
    sets no session cookie, and neither id authenticates."""
    first = await login(replica, await start_login(replica.store))
    assert first.status == 302
    s1 = first.session_id
    entered, release = asyncio.Event(), asyncio.Event()

    async def elevate(request: Request) -> Response:
        entered.set()
        await release.wait()
        request.state.session.rotate_id()
        request.state.session.set_attribute("elevated", True)
        return Response(status_code=200)

    r_request = app_request(s1)
    r_task = asyncio.create_task(replica.session_filter.do_filter(r_request, elevate))
    await asyncio.wait_for(entered.wait(), 10)
    if revocation == "eviction":
        assert (await login(replica, await start_login(replica.store))).status == 302
    else:
        assert (await logout(replica, s1, revocation)).status_code == 302
    release.set()
    r_response: Response = await asyncio.wait_for(r_task, 10)

    s2 = r_request.state.session.id
    assert s2 != s1
    assert not await replica.store.exists(s2), f"{revocation}: the revoked session is back under its new id"
    assert not await replica.store.exists(s1)
    assert session_cookies(r_response) == [], f"{revocation}: the response sets the session cookie"
    assert await authenticated_as(replica, s2) is None
    assert await authenticated_as(replica, s1) is None


async def a_rotation_moves_a_live_session(replica: Replica) -> None:
    """A request of a live, logged-in session rotates its id (a privilege elevation) and changes it: the session
    moves to the new id, with its data, the old id no longer resolves, and the response sends the new cookie."""
    first = await login(replica, await start_login(replica.store))
    s1 = first.session_id

    async def elevate(request: Request) -> Response:
        request.state.session.rotate_id()
        request.state.session.set_attribute("elevated", True)
        return Response(status_code=200)

    request = app_request(s1)
    response = await replica.session_filter.do_filter(request, elevate)

    s2 = request.state.session.id
    assert session_cookies(response) == [s2]
    assert not await replica.store.exists(s1)
    moved = await replica.store.get(s2)
    assert moved is not None and moved["elevated"] is True
    assert await authenticated_as(replica, s2) == replica.principal
    assert await authenticated_as(replica, s1) is None


async def a_login_stands_when_its_pre_authentication_session_is_gone(replica: Replica) -> None:
    """The pre-authentication session is deleted while the login exchanges the code (it expired, or another
    request of the browser ended it): a login is a fresh authentication, so its rotation still stores the
    session under the new id, and the browser is logged in."""
    pre_auth = await start_login(replica.store)

    async def end_the_pre_authentication_session() -> None:
        await replica.store.delete(pre_auth)

    replica.handler.during_exchange = end_the_pre_authentication_session
    try:
        result = await login(replica, pre_auth)
    finally:
        replica.handler.during_exchange = None

    assert result.status == 302
    assert await replica.store.exists(result.session_id)
    assert await authenticated_as(replica, result.session_id) == replica.principal
