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
"""OAuth2 grant scenarios every token store must pass (WP10b): one body, run on each store.

Each scenario drives a real :class:`AuthorizationServer` over the store it is given: N concurrent
redemptions of one refresh token, of one authorization code and of one pushed ``request_uri`` must give
exactly one success, and the losers of a refresh or code race trip the reuse defense (the family issued is
revoked). The SQL matrix (``test_oauth2_token_store_matrix.py``), the Redis suite
(``test_token_store_integration.py``) and the in-memory unit tests use them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable
from typing import Any

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.authorization_server import AuthorizationServer
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository

CONCURRENCY = 20
SECRET = "wp10b-signing-secret-that-is-long-enough-for-hs256"
REDIRECT_URI = "https://app.example.com/cb"
VERIFIER = "v" * 64

SERVICE = ClientRegistration(
    registration_id="svc",
    client_id="svc",
    client_secret="svc-secret",
    authorization_grant_type="client_credentials",
    scopes=["read"],
)
WEB = ClientRegistration(
    registration_id="web",
    client_id="web",
    client_secret="web-secret",
    authorization_grant_type="authorization_code",
    redirect_uri=REDIRECT_URI,
    scopes=["openid", "read"],
)


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def server(store: Any, **options: Any) -> AuthorizationServer:
    """An authorization server over *store*, with a ``client_credentials`` client and a web client."""
    return AuthorizationServer(
        secret=SECRET,
        client_repository=InMemoryClientRegistrationRepository(SERVICE, WEB),
        token_store=store,
        **options,
    )


async def attempt(call: Awaitable[Any]) -> Any:
    """The call's result, or the :class:`SecurityException` it raised."""
    try:
        return await call
    except SecurityException as refused:
        return refused


async def issue(authorization_server: AuthorizationServer) -> str:
    """A fresh refresh token of the service client (a new family)."""
    response = await authorization_server.token(
        grant_type="client_credentials", client_id="svc", client_secret="svc-secret"
    )
    return str(response["refresh_token"])


async def refresh(authorization_server: AuthorizationServer, token: str, *, client_id: str = "svc") -> dict[str, Any]:
    secret = "svc-secret" if client_id == "svc" else "web-secret"
    return await authorization_server.token(
        grant_type="refresh_token", client_id=client_id, client_secret=secret, refresh_token=token
    )


async def code_for(authorization_server: AuthorizationServer) -> str:
    result = await authorization_server.authorize(
        client_id="web", redirect_uri=REDIRECT_URI, user_id="alice", scope="openid read", code_challenge=s256(VERIFIER)
    )
    return str(result["code"])


async def redeem(authorization_server: AuthorizationServer, code: str) -> dict[str, Any]:
    return await authorization_server.token(
        grant_type="authorization_code",
        client_id="web",
        client_secret="web-secret",
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=VERIFIER,
    )


async def is_active(authorization_server: AuthorizationServer, refresh_token: str) -> bool:
    return bool((await authorization_server.introspect(refresh_token))["active"])


def split(outcomes: list[Any]) -> tuple[list[dict[str, Any]], list[SecurityException]]:
    granted = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, SecurityException)]
    assert len(granted) + len(refused) == len(outcomes), f"unexpected outcomes: {outcomes}"
    return granted, refused


# ---------------------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------------------


async def one_refresh_token_is_redeemed_once(store: Any, *, concurrency: int = CONCURRENCY) -> None:
    """C073: N concurrent redemptions of one refresh token rotate it once; the others are replays."""
    authorization_server = server(store)
    token = await issue(authorization_server)

    outcomes = await asyncio.gather(*(attempt(refresh(authorization_server, token)) for _ in range(concurrency)))

    granted, refused = split(outcomes)
    assert len(granted) == 1, f"{len(granted)} requests redeemed one refresh token"
    assert all(error.code == "INVALID_GRANT" for error in refused)
    assert any("reuse" in str(error).lower() for error in refused), [str(error) for error in refused]
    # The replays revoked the family: the one token the race issued is dead too (RFC 9700).
    assert not await is_active(authorization_server, granted[0]["refresh_token"])
    assert not await is_active(authorization_server, token)


async def one_code_is_redeemed_once(store: Any, *, concurrency: int = CONCURRENCY) -> None:
    """C074: N concurrent redemptions of one authorization code succeed once, and the replays revoke what it
    issued (RFC 6749 section 4.1.2)."""
    authorization_server = server(store)
    code = await code_for(authorization_server)

    outcomes = await asyncio.gather(*(attempt(redeem(authorization_server, code)) for _ in range(concurrency)))

    granted, refused = split(outcomes)
    assert len(granted) == 1, f"{len(granted)} requests redeemed one authorization code"
    assert all(error.code == "INVALID_GRANT" for error in refused)
    assert not await is_active(authorization_server, granted[0]["refresh_token"])


async def one_pushed_request_is_consumed_once(store: Any, *, concurrency: int = CONCURRENCY) -> None:
    """C074: a pushed request_uri (RFC 9126) is one-time under concurrency."""
    authorization_server = server(store)
    pushed = await authorization_server.pushed_authorization_request("web", {"scope": "read", "state": "s"})

    results = await asyncio.gather(
        *(authorization_server.consume_pushed_request(pushed["request_uri"], "web") for _ in range(concurrency))
    )

    assert [result for result in results if result is not None] == [{"scope": "read", "state": "s"}]


async def a_request_uri_belongs_to_its_client(store: Any) -> None:
    authorization_server = server(store)
    pushed = await authorization_server.pushed_authorization_request("web", {"scope": "read"})

    assert await authorization_server.consume_pushed_request(pushed["request_uri"], "svc") is None
    assert await authorization_server.consume_pushed_request(pushed["request_uri"], "web") == {"scope": "read"}
    assert await authorization_server.consume_pushed_request(pushed["request_uri"], "web") is None


async def rotation_chain_and_replay(store: Any) -> None:
    """Sequential semantics: a chain of rotations works, and replaying any rotated token revokes the family."""
    authorization_server = server(store)
    first = await issue(authorization_server)
    chain = [first]
    for _ in range(5):
        chain.append((await refresh(authorization_server, chain[-1]))["refresh_token"])
    assert await is_active(authorization_server, chain[-1])
    assert not await is_active(authorization_server, chain[-2])

    replay = await attempt(refresh(authorization_server, chain[2]))
    assert isinstance(replay, SecurityException) and "reuse" in str(replay).lower()
    assert not await is_active(authorization_server, chain[-1])
    refused = await attempt(refresh(authorization_server, chain[-1]))
    assert isinstance(refused, SecurityException) and refused.code == "INVALID_GRANT"


async def a_wrong_client_does_not_consume_the_token(store: Any) -> None:
    authorization_server = server(store)
    token = await issue(authorization_server)

    mismatch = await attempt(refresh(authorization_server, token, client_id="web"))

    assert isinstance(mismatch, SecurityException) and mismatch.code == "INVALID_GRANT"
    assert "refresh_token" in await refresh(authorization_server, token)


async def a_code_replayed_later_revokes_what_it_issued(store: Any) -> None:
    authorization_server = server(store)
    code = await code_for(authorization_server)
    tokens = await redeem(authorization_server, code)
    rotated = (await refresh(authorization_server, tokens["refresh_token"], client_id="web"))["refresh_token"]

    replay = await attempt(redeem(authorization_server, code))

    assert isinstance(replay, SecurityException) and replay.code == "INVALID_GRANT"
    assert not await is_active(authorization_server, rotated)


async def revocation_revokes_the_family(store: Any) -> None:
    authorization_server = server(store)
    first = await issue(authorization_server)
    second = (await refresh(authorization_server, first))["refresh_token"]

    await authorization_server.revoke(first, requesting_client_id="web")  # not the owner: refused silently
    assert await is_active(authorization_server, second)
    await authorization_server.revoke(first, requesting_client_id="svc")

    assert not await is_active(authorization_server, second)
    refused = await attempt(refresh(authorization_server, second))
    assert isinstance(refused, SecurityException) and refused.code == "INVALID_GRANT"


async def expired_grants_are_refused(store: Any) -> None:
    authorization_server = server(store, refresh_token_ttl=-1, auth_code_ttl=-1)
    token = await issue(authorization_server)
    expired = await attempt(refresh(authorization_server, token))
    assert isinstance(expired, SecurityException) and "expired" in str(expired).lower()
    code = await code_for(authorization_server)
    expired_code = await attempt(redeem(authorization_server, code))
    assert isinstance(expired_code, SecurityException) and "expired" in str(expired_code).lower()


async def only_a_refresh_token_is_one(store: Any) -> None:
    """A code or a pushed request_uri presented as a refresh token is unknown. The key-value layout keys a
    refresh token by its bare id and the other records by ``kind:id``, so introspecting ``par:<request_uri>``
    or ``authcode:<code>`` reported an active refresh token."""
    authorization_server = server(store)
    code = await code_for(authorization_server)
    pushed = await authorization_server.pushed_authorization_request("web", {"scope": "read"})

    for impostor in (f"authcode:{code}", f"par:{pushed['request_uri']}"):
        assert not await is_active(authorization_server, impostor), impostor
        refused = await attempt(refresh(authorization_server, impostor, client_id="web"))
        assert isinstance(refused, SecurityException) and refused.code == "INVALID_GRANT"

    assert await authorization_server.consume_pushed_request(pushed["request_uri"], "web") == {"scope": "read"}
    assert "refresh_token" in await redeem(authorization_server, code)


SEQUENTIAL_SCENARIOS = (
    a_request_uri_belongs_to_its_client,
    rotation_chain_and_replay,
    a_wrong_client_does_not_consume_the_token,
    a_code_replayed_later_revokes_what_it_issued,
    revocation_revokes_the_family,
    expired_grants_are_refused,
    only_a_refresh_token_is_one,
)
"""The scenarios without concurrency, which every store (atomic or not) passes."""
