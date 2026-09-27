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
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.concurrency import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
)
from pyfly.session.filter import SessionFilter


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


class _IdpStubbedHandler(OAuth2LoginHandler):
    """The real login handler, with the identity provider's token and userinfo calls answered locally."""

    async def _exchange_code(self, registration: Any, code: str, code_verifier: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"access_token": f"at-{code}"}

    async def _fetch_user_info(self, registration: Any, access_token: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"sub": "alice"}


def _handler(controller: SessionConcurrencyController) -> OAuth2LoginHandler:
    registration = ClientRegistration(
        registration_id="idp",
        client_id="app",
        client_secret="app-secret",
        redirect_uri="https://app/cb",
        scopes=["openid"],
        authorization_uri="https://idp/auth",
        token_uri="https://idp/token",
        use_pkce=False,
    )
    return _IdpStubbedHandler(InMemoryClientRegistrationRepository(registration), concurrency=controller)


async def _login(session_filter: SessionFilter, handler: OAuth2LoginHandler, pre_auth_session: str) -> int:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/login/oauth2/code/idp",
        "headers": [(b"cookie", f"PYFLY_SESSION={pre_auth_session}".encode())],
        "query_string": f"state=state-{pre_auth_session}&code=code-{pre_auth_session}".encode(),
        "path_params": {"registration_id": "idp"},
    }

    async def call_next(request: Request) -> Response:
        return await handler._handle_callback(request)

    response = await session_filter.do_filter(Request(scope), call_next)
    return int(response.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [2, 8])
async def test_max_sessions_one_admits_one_of_concurrent_oauth2_logins(concurrent: int) -> None:
    store = _YieldingStore()
    controller = SessionConcurrencyController(
        InMemorySessionRegistry(),
        ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"),
        session_store=store,
    )
    session_filter = SessionFilter(store=store)
    handler = _handler(controller)
    pre_auth = [f"pre-{index}" for index in range(concurrent)]
    for session_id in pre_auth:
        await store.save(session_id, {"oauth2_state": f"state-{session_id}"}, ttl=600)

    statuses = await asyncio.gather(*(_login(session_filter, handler, session_id) for session_id in pre_auth))

    assert statuses.count(302) == 1, statuses
    assert statuses.count(401) == concurrent - 1
    assert await controller.registry.count("alice") == 1


@pytest.mark.asyncio
async def test_a_login_after_the_sessions_expired_is_admitted() -> None:
    """C076 end to end: the user closed the browser (no logout) and the session expired."""
    store = _YieldingStore()
    controller = SessionConcurrencyController(
        InMemorySessionRegistry(),
        ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"),
        session_store=store,
    )
    session_filter = SessionFilter(store=store)
    handler = _handler(controller)
    await store.save("pre-a", {"oauth2_state": "state-pre-a"}, ttl=600)
    assert await _login(session_filter, handler, "pre-a") == 302
    [(first, _created)] = await controller.registry.list_sessions("alice")
    await store.save(first, {}, ttl=-1)  # expired

    await store.save("pre-b", {"oauth2_state": "state-pre-b"}, ttl=600)
    assert await _login(session_filter, handler, "pre-b") == 302
    assert [sid for sid, _ in await controller.registry.list_sessions("alice")] != [first]
