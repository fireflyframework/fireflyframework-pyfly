# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""One persisted authorization request admits at most one provider exchange."""

from __future__ import annotations

import asyncio
import os
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request
from starlette.responses import Response

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.session.adapters.sql_session_store import SqlSessionStore
from pyfly.session.filter import SessionFilter
from tests.security.test_oauth2_resource_server import _mint
from tests.security.test_oauth2_resource_server import jwks as jwks


def incoming(*, cookie: str = "", query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/callback",
            "headers": [(b"cookie", f"PYFLY_SESSION={cookie}".encode())] if cookie else [],
            "query_string": query.encode(),
            "path_params": {"registration_id": "acme"},
        }
    )


@pytest.mark.parametrize("backend", ["memory", "sql", "postgres", "redis"])
async def test_same_challenge_is_consumed_before_concurrent_token_exchanges(tmp_path: Path, jwks, backend) -> None:
    uri, issuer, _ = jwks
    registration = ClientRegistration(
        "acme",
        "client",
        client_secret="fixture-client-secret-not-loggable",
        authorization_uri=issuer + "/authorize",
        token_uri=issuer + "/token",
        redirect_uri="https://app.test/callback",
        jwks_uri=uri,
        issuer_uri=issuer,
        scopes=["openid"],
    )
    repository = InMemoryClientRegistrationRepository(registration)
    engine = None
    client = None
    if backend == "memory":
        from pyfly.session.adapters.memory import InMemorySessionStore

        shared = InMemorySessionStore()
        stores = [shared, shared]
    elif backend == "redis":
        import redis.asyncio as aioredis

        from pyfly.session.adapters.redis import RedisSessionStore

        if not os.environ.get("PYFLY_IT_REDIS_URL"):
            pytest.skip("Owned Redis endpoint not configured")
        client = aioredis.from_url(os.environ["PYFLY_IT_REDIS_URL"])
        stores = [RedisSessionStore(client), RedisSessionStore(client)]
    else:
        if backend == "postgres" and not os.environ.get("PYFLY_IT_POSTGRES_URL"):
            pytest.skip("Owned PostgreSQL endpoint not configured")
        url = (
            os.environ["PYFLY_IT_POSTGRES_URL"]
            if backend == "postgres"
            else f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}"
        )
        engine = create_async_engine(url)
        table = "claim_" + uuid.uuid4().hex
        stores = [SqlSessionStore(engine, table=table), SqlSessionStore(engine, table=table)]
        await stores[0].start()
        await stores[1].start()
    filters = [SessionFilter(store) for store in stores]
    handlers = [OAuth2LoginHandler(repository, require_oidc_id_token=True) for _ in stores]
    first_exchange, loser_settled, release_provider = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loaded = asyncio.Barrier(2)
    exchanges: list[bool] = []
    callbacks: list[asyncio.Task] = []
    try:
        authorization = await filters[0].do_filter(incoming(), handlers[0]._handle_authorization)
        cookies = SimpleCookie()
        cookies.load(authorization.headers["set-cookie"])
        session_id = cookies["PYFLY_SESSION"].value
        parameters = parse_qs(urlparse(authorization.headers["location"]).query)
        state, nonce = parameters["state"][0], parameters["nonce"][0]
        stored = await stores[0].get(session_id)
        verifier = stored["oauth2_pkce_verifier"]
        token = _mint({"iss": issuer, "aud": "client", "sub": "alice", "nonce": nonce})

        async def provider(request: httpx.Request) -> httpx.Response:
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == [verifier]
            exchanges.append(True)
            first_exchange.set()
            if len(exchanges) > 1:
                loser_settled.set()
            await release_provider.wait()
            # Deliberately accepts duplicate codes: provider rejection is not the gate under test.
            return httpx.Response(200, json={"access_token": "fixture-access-token", "id_token": token})

        async def callback(index: int):
            async def handler(request: Request):
                await loaded.wait()
                return await handlers[index]._handle_callback(request)

            try:
                return await filters[index].do_filter(
                    incoming(cookie=session_id, query=urlencode({"state": state, "code": "same-code"})), handler
                )
            finally:
                loser_settled.set()

        with respx.mock(assert_all_mocked=True) as mock:
            mock.post(registration.token_uri).mock(side_effect=provider)
            callbacks = [asyncio.create_task(callback(index)) for index in range(2)]
            try:
                await asyncio.wait_for(first_exchange.wait(), 5)
                await asyncio.wait_for(loser_settled.wait(), 5)
                assert len(exchanges) == 1, "One stored challenge reached token exchange more than once"
            finally:
                release_provider.set()
                responses = await asyncio.wait_for(asyncio.gather(*callbacks), 5)
        assert sorted(response.status_code for response in responses) == [302, 400]
        assert await stores[0].get(session_id) is None
    finally:
        release_provider.set()
        for task in callbacks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*callbacks, return_exceptions=True)
        if engine is not None:
            await engine.dispose()
        if client is not None:
            await client.aclose()


@pytest.fixture(params=["memory", "sql", "postgres", "redis"])
async def callback_flow(request, tmp_path):
    from pyfly.session.adapters.memory import InMemorySessionStore

    engine = None
    if request.param == "memory":
        store = InMemorySessionStore()
    elif request.param == "redis":
        import redis.asyncio as aioredis

        from pyfly.session.adapters.redis import RedisSessionStore

        if not os.environ.get("PYFLY_IT_REDIS_URL"):
            pytest.skip("Owned Redis endpoint not configured")
        client = aioredis.from_url(os.environ["PYFLY_IT_REDIS_URL"])
        store = RedisSessionStore(client)
    else:
        if request.param == "postgres" and not os.environ.get("PYFLY_IT_POSTGRES_URL"):
            pytest.skip("Owned PostgreSQL endpoint not configured")
        url = (
            os.environ["PYFLY_IT_POSTGRES_URL"]
            if request.param == "postgres"
            else f"sqlite+aiosqlite:///{tmp_path / 'callback-flow.db'}"
        )
        engine = create_async_engine(url)
        store = SqlSessionStore(engine, table="claim_" + uuid.uuid4().hex)
        await store.start()
    registration = ClientRegistration(
        "acme",
        "client",
        client_secret="fixture-secret",
        authorization_uri="https://issuer.test/authorize",
        token_uri="https://issuer.test/token",
        user_info_uri="https://issuer.test/userinfo",
        redirect_uri="https://app.test/callback",
    )
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(registration))
    filter = SessionFilter(store)
    authorization = await filter.do_filter(incoming(), handler._handle_authorization)
    cookies = SimpleCookie()
    cookies.load(authorization.headers["set-cookie"])
    old_id = cookies["PYFLY_SESSION"].value
    state = parse_qs(urlparse(authorization.headers["location"]).query)["state"][0]
    try:
        yield store, filter, handler, old_id, state
    finally:
        if engine is not None:
            await engine.dispose()
        if request.param == "redis":
            await client.aclose()


async def test_provider_error_consumes_entire_challenge_but_preserves_current_server_attrs(callback_flow):
    store, filter, handler, old_id, state = callback_flow
    data = await store.get(old_id)
    await store.replace(old_id, {**data, "unrelated": {"empty": []}}, 60)
    request = incoming(cookie=old_id, query=urlencode({"state": state, "error": "access_denied"}))
    response = await filter.do_filter(request, handler._handle_callback)
    assert response.status_code == 400
    assert await store.get(old_id) is None
    continuation = await store.get(request.state.session.id)
    assert continuation["unrelated"] == {"empty": []}
    assert all(name not in continuation for name in ("oauth2_state", "oauth2_nonce", "oauth2_pkce_verifier"))
    replay = await filter.do_filter(
        request := incoming(cookie=old_id, query=urlencode({"state": state, "code": "code"})), handler._handle_callback
    )
    assert replay.status_code == 400


@pytest.mark.parametrize("when", ["before-claim", "continuation", "continuation-expired", "old-cookie-after-claim"])
async def test_logout_claim_linearization_and_conditional_authenticated_publication(callback_flow, when):
    store, filter, handler, old_id, state = callback_flow
    entered, release = asyncio.Event(), asyncio.Event()
    request = incoming(cookie=old_id, query=urlencode({"state": state, "code": "code"}))
    calls = []

    async def provider(_request):
        calls.append(True)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "fixture-access-token"})

    async def callback(inner):
        if when == "before-claim":
            # A second real native request logs out after this callback loaded its stale challenge.
            await filter.do_filter(incoming(cookie=old_id), handler._handle_logout)
        return await handler._handle_callback(inner)

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://issuer.test/token").mock(side_effect=provider)
        mock.get("https://issuer.test/userinfo").respond(200, json={"sub": "fresh-signed-in-user"})
        task = asyncio.create_task(filter.do_filter(request, callback))
        try:
            if when != "before-claim":
                await asyncio.wait_for(entered.wait(), 2)
                assert await store.get(old_id) is None
                target = request.state.session.id if when.startswith("continuation") else old_id
                if when == "continuation-expired":
                    await store.save(target, await store.get(target), 1)
                    await asyncio.sleep(1.05)
                    assert await store.get(target) is None
                else:
                    await filter.do_filter(incoming(cookie=target), handler._handle_logout)
            release.set()
            response = await asyncio.wait_for(task, 3)
            if when == "before-claim":
                assert response.status_code == 400 and not calls
                assert "set-cookie" not in response.headers
            elif when.startswith("continuation"):
                assert response.status_code == 401 and len(calls) == 1
                assert "set-cookie" not in response.headers
                assert await store.get(request.state.session.id) is None
            else:
                # Retired old-cookie logout is not a global revocation of independently claimed auth.
                assert response.status_code == 302 and len(calls) == 1
                assert (await store.get(request.state.session.id))["SECURITY_CONTEXT"].user_id == "fresh-signed-in-user"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_stale_ordinary_request_cannot_rearm_consumed_challenge(callback_flow):
    store, filter, handler, old_id, state = callback_flow
    loaded, release = asyncio.Event(), asyncio.Event()

    async def ordinary(request):
        loaded.set()
        await release.wait()
        request.state.session.set_attribute("ordinary-write", True)
        return Response()

    task = asyncio.create_task(filter.do_filter(incoming(cookie=old_id), ordinary))
    try:
        await asyncio.wait_for(loaded.wait(), 2)
        await filter.do_filter(
            incoming(cookie=old_id, query=urlencode({"state": state, "error": "access_denied"})),
            handler._handle_callback,
        )
        release.set()
        response = await asyncio.wait_for(task, 2)
        assert "set-cookie" not in response.headers
        assert await store.get(old_id) is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_unsupported_sql_claim_fails_before_any_provider_exchange(callback_flow):
    store, filter, handler, old_id, state = callback_flow
    if not isinstance(store, SqlSessionStore):
        pytest.skip("SQL unsupported-backend contract")
    store._dialect = "mysql"  # This unqualified SQL dialect must never reach claim SQL/provider I/O.
    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.post("https://issuer.test/token").respond(200, json={"access_token": "forbidden"})
        response = await filter.do_filter(
            incoming(cookie=old_id, query=urlencode({"state": state, "code": "code"})), handler._handle_callback
        )
        assert response.status_code == 400
        assert not endpoint.called
        assert "set-cookie" not in response.headers
    store._dialect = None
    assert (await store.get(old_id))["oauth2_state"] == state


async def test_missing_claim_hook_never_uses_a_local_snapshot_as_consumption():
    from pyfly.session import HttpSession

    request = incoming(query="state=state&code=code")
    request.state.session = HttpSession("not-persisted", {"oauth2_state": "state"})
    registration = ClientRegistration("acme", "client", token_uri="https://issuer.test/token")
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(registration))
    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.post(registration.token_uri).respond(200, json={"access_token": "forbidden"})
        response = await handler._handle_callback(request)
        assert response.status_code == 400 and not endpoint.called


async def test_claim_adopts_current_store_snapshot_instead_of_stale_request_authority(callback_flow):
    from pyfly.security.context import SecurityContext

    store, filter, handler, old_id, state = callback_flow
    initial = await store.get(old_id)
    await store.replace(old_id, {**initial, "SECURITY_CONTEXT": SecurityContext(user_id="stale-user")}, 60)
    request = incoming(cookie=old_id, query=urlencode({"state": state, "error": "access_denied"}))

    async def changed_after_load(inner):
        current = await store.get(old_id)
        await store.replace(old_id, {**current, "SECURITY_CONTEXT": None, "server-current": True}, 60)
        return await handler._handle_callback(inner)

    assert (await filter.do_filter(request, changed_after_load)).status_code == 400
    persisted = await store.get(request.state.session.id)
    assert persisted.get("SECURITY_CONTEXT") is None and persisted["server-current"] is True
    assert request.state.session.get_attribute("SECURITY_CONTEXT") is None


async def test_store_without_atomic_capability_fails_closed_through_real_filter():
    from pyfly.session.adapters.memory import InMemorySessionStore

    class LegacyStore:
        def __init__(self):
            self.delegate = InMemorySessionStore()

        async def get(self, session_id):
            return await self.delegate.get(session_id)

        async def save(self, session_id, data, ttl):
            await self.delegate.save(session_id, data, ttl)

        async def delete(self, session_id):
            await self.delegate.delete(session_id)

        async def exists(self, session_id):
            return await self.delegate.exists(session_id)

    store = LegacyStore()
    await store.save("old", {"oauth2_state": "state"}, 60)
    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(
            ClientRegistration("acme", "client", token_uri="https://issuer.test/token")
        )
    )
    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.post("https://issuer.test/token").respond(200, json={"access_token": "forbidden"})
        response = await SessionFilter(store).do_filter(
            incoming(cookie="old", query="state=state&code=code"), handler._handle_callback
        )
        assert response.status_code == 400 and not endpoint.called
        assert "set-cookie" not in response.headers
        assert (await store.get("old"))["oauth2_state"] == "state"


async def test_claim_storage_exception_never_logs_secret_or_publishes_stale_cookie(caplog):
    from pyfly.session.adapters.memory import InMemorySessionStore

    canary = "fixture-secret-exception-never-log"

    class BrokenStore(InMemorySessionStore):
        async def claim_attributes(self, *args, **kwargs):
            raise RuntimeError(canary)

    store = BrokenStore()
    await store.save("old", {"oauth2_state": "state"}, 60)
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(ClientRegistration("acme", "client")))
    response = await SessionFilter(store).do_filter(
        incoming(cookie="old", query="state=state&code=code"), handler._handle_callback
    )
    assert response.status_code == 400 and "set-cookie" not in response.headers
    assert canary not in caplog.text and canary.encode() not in response.body


@pytest.mark.parametrize("reauthenticate", [False, True])
async def test_cancelled_claim_never_rearms_retired_challenge_or_publishes_identity(reauthenticate):
    from pyfly.session.adapters.memory import InMemorySessionStore

    claimed, release = asyncio.Event(), asyncio.Event()
    continuation = []

    class HeldStore(InMemorySessionStore):
        async def claim_attributes(self, old_id, new_id, expected, names, ttl):
            result = await super().claim_attributes(old_id, new_id, expected, names, ttl)
            continuation.append(new_id)
            claimed.set()
            await release.wait()
            return result

    store = HeldStore()
    from pyfly.security.context import SecurityContext

    data = {"oauth2_state": "state", "oauth2_nonce": "nonce"}
    if reauthenticate:
        data["SECURITY_CONTEXT"] = SecurityContext(user_id="prior-user")
    await store.save("old", data, 60)
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(ClientRegistration("acme", "client")))
    task = asyncio.create_task(
        SessionFilter(store).do_filter(incoming(cookie="old", query="state=state&code=code"), handler._handle_callback)
    )
    try:
        await asyncio.wait_for(claimed.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.get("old") is None
        remaining = await store.get(continuation[0])
        assert all(name not in remaining for name in ("oauth2_state", "oauth2_nonce", "SECURITY_CONTEXT"))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_uncertain_final_rename_never_leaves_unregistered_authenticated_session():
    from pyfly.session.adapters.memory import InMemorySessionStore

    class UncertainStore(InMemorySessionStore):
        async def rename(self, old_id, new_id, data, ttl):
            await super().rename(old_id, new_id, data, ttl)
            raise RuntimeError("fixture uncertain final commit")

    store = UncertainStore()
    await store.save("old", {"oauth2_state": "state", "oauth2_nonce": "nonce"}, 60)
    request = incoming(cookie="old", query="state=state&code=code")
    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(
            ClientRegistration(
                "acme",
                "client",
                client_secret="fixture-secret",
                token_uri="https://issuer.test/token",
                user_info_uri="https://issuer.test/userinfo",
            )
        )
    )
    with respx.mock() as mock:
        mock.post("https://issuer.test/token").respond(200, json={"access_token": "fixture-token"})
        mock.get("https://issuer.test/userinfo").respond(200, json={"sub": "fresh-user"})
        with pytest.raises(RuntimeError, match="fixture uncertain final commit"):
            await SessionFilter(store).do_filter(request, handler._handle_callback)
    assert not store._store, "A final commit error left a live authenticated session behind"


async def test_uncertain_committed_claim_cannot_leave_prior_authentication_on_unadopted_continuation():
    from pyfly.security.context import SecurityContext
    from pyfly.session.adapters.memory import InMemorySessionStore

    class UncertainStore(InMemorySessionStore):
        async def claim_attributes(self, *args, **kwargs):
            await super().claim_attributes(*args, **kwargs)
            raise RuntimeError("fixture uncertain claim commit")

    store = UncertainStore()
    await store.save(
        "old",
        {"oauth2_state": "state", "oauth2_nonce": "nonce", "SECURITY_CONTEXT": SecurityContext(user_id="prior-user")},
        60,
    )
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(ClientRegistration("acme", "client")))
    response = await SessionFilter(store).do_filter(
        incoming(cookie="old", query="state=state&code=code"), handler._handle_callback
    )
    assert response.status_code == 400 and "set-cookie" not in response.headers
    assert await store.get("old") is None
    assert len(store._store) == 1
    assert all("SECURITY_CONTEXT" not in data for data, _ in store._store.values())


async def test_completed_reauthentication_failure_preserves_current_authenticated_context(callback_flow):
    from pyfly.security.context import SecurityContext

    store, filter, handler, old_id, state = callback_flow
    current = await store.get(old_id)
    await store.replace(old_id, {**current, "SECURITY_CONTEXT": SecurityContext(user_id="current-user")}, 60)
    request = incoming(cookie=old_id, query=urlencode({"state": state, "error": "access_denied"}))
    response = await filter.do_filter(request, handler._handle_callback)
    assert response.status_code == 400
    assert (await store.get(request.state.session.id))["SECURITY_CONTEXT"].user_id == "current-user"


@pytest.mark.parametrize(
    "competing_login,failure", [(False, "provider-error"), (False, "token-error"), (True, "token-error")]
)
async def test_failed_reauthentication_restoration_obeys_session_cap(competing_login, failure):
    import time

    from pyfly.security.context import SecurityContext
    from pyfly.session import ConcurrencyControlPolicy, InMemorySessionRegistry, SessionConcurrencyController
    from pyfly.session.adapters.memory import InMemorySessionStore

    store = InMemorySessionStore()
    controller = SessionConcurrencyController(
        InMemorySessionRegistry(), ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"), session_store=store
    )
    await store.save("old", {"oauth2_state": "state", "SECURITY_CONTEXT": SecurityContext(user_id="user")}, 60)
    assert await controller.on_login("user", "old", time.time())
    handler = OAuth2LoginHandler(
        InMemoryClientRegistrationRepository(
            ClientRegistration("acme", "client", token_uri="https://issuer.test/token")
        ),
        concurrency=controller,
    )
    query = "state=state&error=access_denied" if failure == "provider-error" else "state=state&code=code"
    request = incoming(cookie="old", query=query)
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(_request):
        entered.set()
        await release.wait()
        return httpx.Response(400, json={"error": "invalid_grant"})

    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.post("https://issuer.test/token").mock(side_effect=provider)
        task = asyncio.create_task(SessionFilter(store).do_filter(request, handler._handle_callback))
        try:
            if failure == "token-error":
                await asyncio.wait_for(entered.wait(), 2)
            if competing_login:
                await store.save("competitor", {"SECURITY_CONTEXT": SecurityContext(user_id="user")}, 60)
                assert await controller.on_login("user", "competitor", time.time())
            release.set()
            response = await asyncio.wait_for(task, 2)
            if competing_login:
                assert response.status_code == 401
                assert await store.get(request.state.session.id) is None
                assert await store.get("competitor") is not None
            else:
                assert response.status_code == (400 if failure == "provider-error" else 502)
                assert request.state.session.id in {sid for sid, _ in await controller.registry.list_sessions("user")}
                await store.save("competitor", {"SECURITY_CONTEXT": SecurityContext(user_id="user")}, 60)
                assert not await controller.on_login("user", "competitor", time.time())
                await store.delete("competitor")  # The rejected login's native filter normally removes it.
            assert endpoint.called == (failure == "token-error")
            assert await controller.registry.count("user") == 1
            live = [
                data for data, _ in store._store.values() if isinstance(data.get("SECURITY_CONTEXT"), SecurityContext)
            ]
            assert len(live) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_revoked_continuation_cannot_restore_prior_authentication_on_failed_reauth(callback_flow):
    from pyfly.security.context import SecurityContext

    store, filter, handler, old_id, state = callback_flow
    current = await store.get(old_id)
    await store.replace(old_id, {**current, "SECURITY_CONTEXT": SecurityContext(user_id="current-user")}, 60)
    request = incoming(cookie=old_id, query=urlencode({"state": state, "code": "code"}))
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(_request):
        entered.set()
        await release.wait()
        return httpx.Response(400, json={"error": "invalid_grant"})

    with respx.mock() as mock:
        mock.post("https://issuer.test/token").mock(side_effect=provider)
        task = asyncio.create_task(filter.do_filter(request, handler._handle_callback))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            continuation = request.state.session.id
            assert (await store.get(continuation)).get("SECURITY_CONTEXT") is None
            await filter.do_filter(incoming(cookie=continuation), handler._handle_logout)
            release.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 401 and "set-cookie" not in response.headers
            assert await store.get(continuation) is None and await store.get(old_id) is None
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
