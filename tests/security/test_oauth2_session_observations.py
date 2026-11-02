# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Atomic callback publication preserves honest authentication observations."""

from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import respx

from pyfly.security.context import SecurityContext
from pyfly.session import (
    AtomicSessionStore,
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
    SessionExpiryStore,
)
from tests.security.test_oauth2_callback_consumption import callback_flow as callback_flow
from tests.security.test_oauth2_callback_consumption import incoming


async def prior_authentication(flow, authenticated_at):
    store, _filter, handler, old_id, _state = flow
    data = await store.get(old_id)
    data = {**data, "SECURITY_CONTEXT": SecurityContext(user_id="user")}
    if authenticated_at is None:
        data.pop("_authenticated_at", None)
    else:
        data["_authenticated_at"] = authenticated_at
    assert await store.replace(old_id, data, 60)
    controller = SessionConcurrencyController(
        InMemorySessionRegistry(), ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"), session_store=store
    )
    assert await controller.on_login("user", old_id, data["_created_at"])
    handler._concurrency = controller
    return controller, data["_created_at"]


@pytest.mark.parametrize("previous_authenticated_at", [1234.5, None])
@pytest.mark.parametrize("failure", ["provider-error", "token-error"])
async def test_failed_reauthentication_preserves_recency_and_obeys_cap(
    callback_flow, previous_authenticated_at, failure
):
    store, filter, handler, old_id, state = callback_flow
    controller, created_at = await prior_authentication(callback_flow, previous_authenticated_at)
    assert isinstance(store, AtomicSessionStore) and isinstance(store, SessionExpiryStore)
    query = (
        {"state": state, "error": "access_denied"} if failure == "provider-error" else {"state": state, "code": "code"}
    )
    request = incoming(cookie=old_id, query=urlencode(query))
    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.post("https://issuer.test/token").respond(400, json={"error": "invalid_grant"})
        response = await filter.do_filter(request, handler._handle_callback)

    assert response.status_code == (400 if failure == "provider-error" else 502)
    assert endpoint.called == (failure == "token-error")
    session = request.state.session
    persisted = await store.get(session.id)
    assert session.id != old_id and session.claim_bound
    assert session.authenticated_at == previous_authenticated_at
    assert persisted.get("_authenticated_at") == previous_authenticated_at
    if previous_authenticated_at is None:
        assert "_authenticated_at" not in persisted
    assert session.created_at == created_at
    assert persisted["SECURITY_CONTEXT"].user_id == "user"
    assert await store.expires_at(old_id) is None
    assert await store.expires_at(session.id) is not None
    assert {sid for sid, _ in await controller.registry.list_sessions("user")} == {session.id}

    await store.save("competitor", {"SECURITY_CONTEXT": SecurityContext(user_id="user")}, 60)
    try:
        assert not await controller.on_login("user", "competitor", created_at)
    finally:
        await store.delete("competitor")
    assert await controller.registry.count("user") == 1
    assert (await store.get(session.id)).get("_authenticated_at") == previous_authenticated_at


async def test_verified_fresh_login_advances_authentication_observation(callback_flow, monkeypatch):
    store, filter, handler, old_id, state = callback_flow
    controller, created_at = await prior_authentication(callback_flow, 1234.5)
    monkeypatch.setattr("pyfly.session.session.time", SimpleNamespace(time=lambda: 5678.25))
    request = incoming(cookie=old_id, query=urlencode({"state": state, "code": "code"}))
    with respx.mock() as mock:
        mock.post("https://issuer.test/token").respond(200, json={"access_token": "fixture-token"})
        mock.get("https://issuer.test/userinfo").respond(200, json={"sub": "user"})
        response = await filter.do_filter(request, handler._handle_callback)

    assert response.status_code == 302
    session = request.state.session
    persisted = await store.get(session.id)
    assert session.authenticated_at == persisted["_authenticated_at"] == 5678.25
    assert session.created_at == created_at
    assert persisted["SECURITY_CONTEXT"].user_id == "user"
    assert await store.get(old_id) is None
    assert isinstance(store, AtomicSessionStore) and isinstance(store, SessionExpiryStore)
    assert await store.expires_at(session.id) is not None
    assert {sid for sid, _ in await controller.registry.list_sessions("user")} == {session.id}
