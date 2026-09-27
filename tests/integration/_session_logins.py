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

:func:`concurrent_logins_keep_the_cap` is one scenario, run by the unit tests on the in-memory store and by the
integration tests on every SQL lane and on Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.filter import SessionFilter
from pyfly.session.ports.outbound import SessionStore

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
    login authenticates *principal*."""

    def __init__(self, principal: str, controller: SessionConcurrencyController | None) -> None:
        super().__init__(InMemoryClientRegistrationRepository(_REGISTRATION), concurrency=controller)
        self._principal = principal

    async def _exchange_code(self, registration: Any, code: str, code_verifier: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"access_token": f"at-{code}"}

    async def _fetch_user_info(self, registration: Any, access_token: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"sub": self._principal}


class Replica:
    """One application instance: its session filter and login handler over *store* and *controller* (which
    several replicas share when their store and registry are shared)."""

    def __init__(
        self, store: SessionStore, controller: SessionConcurrencyController | None, *, principal: str = "alice"
    ) -> None:
        self.store = store
        self.controller = controller
        self.principal = principal
        self.session_filter = SessionFilter(store=store)
        self.handler = IdpStubbedHandler(principal, controller)


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
        return await replica.handler._handle_callback(inner)

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
