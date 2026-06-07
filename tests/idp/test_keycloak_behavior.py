# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for :class:`KeycloakIdpAdapter`.

These exercise the adapter against a *fake* httpx client (no network, no
Docker). The adapter obtains its client via the async ``_client()`` helper and
uses it inside ``async with await self._client() as client:`` blocks, so we
inject by monkeypatching ``_client`` on the adapter instance to hand back a
single recording fake. Each test asserts BOTH the outbound request the adapter
built (URL, verb, payload, auth headers) AND that the adapter parsed the canned
response into the right domain object.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.idp.adapters.keycloak import KeycloakIdpAdapter
from pyfly.idp.models import IdpUser, LoginRequest, SessionIntrospection

BASE_URL = "https://keycloak.example.com"
REALM = "demo"
TOKEN_URL = f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token"
ADMIN_USERS = f"{BASE_URL}/admin/realms/{REALM}/users"
INTROSPECT_URL = f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token/introspect"


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # The real httpx raises httpx.HTTPStatusError; for behavior tests a
            # generic exception is enough to prove the adapter does not swallow
            # server errors on the happy-path methods.
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class FakeClient:
    """Records every outbound request and returns canned responses.

    A single instance is returned for *every* ``await self._client()`` call so
    the token fetch in ``_admin_auth_header`` and the subsequent admin call are
    captured on the same recorder.
    """

    def __init__(self, routes: list[tuple[str, FakeResponse]]) -> None:
        # routes: ordered list of (url-substring, response); first match wins.
        self._routes = routes
        self.requests: list[dict[str, Any]] = []

    # async context-manager protocol (`async with await self._client() as c`)
    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def _record(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        for needle, resp in self._routes:
            if needle in url:
                return resp
        msg = f"no canned route for {method} {url}"
        raise AssertionError(msg)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("POST", url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("GET", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("DELETE", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._record(method, url, **kwargs)


def _adapter() -> KeycloakIdpAdapter:
    return KeycloakIdpAdapter(
        base_url=BASE_URL,
        realm=REALM,
        client_id="admin-cli",
        client_secret="s3cr3t",
    )


def _inject(adapter: KeycloakIdpAdapter, fake: FakeClient) -> None:
    """Make every ``await self._client()`` return the same recording fake."""

    async def _fake_client() -> FakeClient:
        return fake

    adapter._client = _fake_client  # type: ignore[method-assign]  # noqa: SLF001


def _find(requests: list[dict[str, Any]], *, method: str, needle: str) -> dict[str, Any]:
    for req in requests:
        if req["method"] == method and needle in req["url"]:
            return req
    msg = f"no {method} request to …{needle} was made (got {requests})"
    raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# create_user — admin token grant + user POST + Location id parsing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_user_builds_admin_request_and_parses_location_id() -> None:
    fake = FakeClient(
        [
            (
                "openid-connect/token",
                FakeResponse(200, json_body={"access_token": "ADMIN-TOK", "expires_in": 300}),
            ),
            (
                "/users",
                FakeResponse(
                    201,
                    headers={"Location": f"{ADMIN_USERS}/abc-123-uuid"},
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.create_user(
        IdpUser(username="alice", email="alice@example.com", first_name="Al", last_name="Ice"),
        password="p@ss-w0rd",
    )

    # (a) outbound: the client_credentials admin token grant came first.
    token_req = _find(fake.requests, method="POST", needle="openid-connect/token")
    assert token_req["url"] == TOKEN_URL
    assert token_req["data"]["grant_type"] == "client_credentials"
    assert token_req["data"]["client_id"] == "admin-cli"
    assert token_req["data"]["client_secret"] == "s3cr3t"

    # (a) outbound: the user-creation POST carries the bearer header + payload.
    create_req = _find(fake.requests, method="POST", needle="/admin/realms/demo/users")
    assert create_req["url"] == ADMIN_USERS
    assert create_req["headers"] == {"Authorization": "Bearer ADMIN-TOK"}
    body = create_req["json"]
    assert body["username"] == "alice"
    assert body["email"] == "alice@example.com"
    assert body["enabled"] is True
    assert body["credentials"] == [{"type": "password", "value": "p@ss-w0rd", "temporary": False}]

    # (b) parsed: id extracted from the Location header tail.
    assert result.id == "abc-123-uuid"
    assert result.username == "alice"


# --------------------------------------------------------------------------- #
# login — password grant, token parsing, find_by_username follow-up
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_password_grant_returns_authresult() -> None:
    fake = FakeClient(
        [
            (
                "openid-connect/token",
                FakeResponse(
                    200,
                    json_body={
                        "access_token": "ACCESS-XYZ",
                        "refresh_token": "REFRESH-ABC",
                        "expires_in": 1800,
                    },
                ),
            ),
            # find_by_username GET on the admin users endpoint
            (
                "/users",
                FakeResponse(200, json_body=[{"id": "user-9", "username": "bob", "email": "bob@x.io"}]),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.login(LoginRequest(username="bob", password="hunter2"))

    # (a) outbound: ROPC password grant with credentials in the form body.
    token_req = _find(fake.requests, method="POST", needle="openid-connect/token")
    assert token_req["url"] == TOKEN_URL
    assert token_req["data"]["grant_type"] == "password"
    assert token_req["data"]["username"] == "bob"
    assert token_req["data"]["password"] == "hunter2"
    assert token_req["data"]["client_id"] == "admin-cli"

    # (a) outbound: the username lookup uses exact match query params.
    lookup_req = _find(fake.requests, method="GET", needle="/admin/realms/demo/users")
    assert lookup_req["params"] == {"username": "bob", "exact": "true"}

    # (b) parsed: tokens + resolved user mapped into AuthResult.
    assert result.access_token == "ACCESS-XYZ"
    assert result.refresh_token == "REFRESH-ABC"
    assert result.expires_in == 1800
    assert result.user.id == "user-9"
    assert result.user.username == "bob"


@pytest.mark.asyncio
async def test_login_invalid_credentials_raises_permission_error() -> None:
    fake = FakeClient(
        [
            (
                "openid-connect/token",
                FakeResponse(401, json_body={"error": "invalid_grant"}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    # error path: a non-200 token response maps to PermissionError, and the
    # adapter must NOT attempt the find_by_username follow-up.
    with pytest.raises(PermissionError):
        await adapter.login(LoginRequest(username="bob", password="wrong"))

    assert all("/admin/realms" not in req["url"] for req in fake.requests)


# --------------------------------------------------------------------------- #
# introspect — token introspection mapped into SessionIntrospection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_introspect_maps_active_session() -> None:
    fake = FakeClient(
        [
            (
                "token/introspect",
                FakeResponse(
                    200,
                    json_body={
                        "active": True,
                        "sub": "user-9",
                        "preferred_username": "bob",
                        "scope": "openid profile email",
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.introspect("ACCESS-XYZ")

    # (a) outbound: introspect endpoint with client creds + token in form body.
    req = _find(fake.requests, method="POST", needle="token/introspect")
    assert req["url"] == INTROSPECT_URL
    assert req["data"] == {
        "client_id": "admin-cli",
        "client_secret": "s3cr3t",
        "token": "ACCESS-XYZ",
    }

    # (b) parsed: domain SessionIntrospection with split scopes.
    assert isinstance(result, SessionIntrospection)
    assert result.active is True
    assert result.user_id == "user-9"
    assert result.username == "bob"
    assert result.scopes == ["openid", "profile", "email"]
