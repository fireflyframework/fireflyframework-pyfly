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
"""Behavior tests for :class:`AzureAdIdpAdapter`.

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

from pyfly.idp.adapters.azure_ad import AzureAdIdpAdapter
from pyfly.idp.models import IdpRole, IdpUser, LoginRequest

TENANT_ID = "tenant-abc"
CLIENT_ID = "app-client-id"
CLIENT_SECRET = "app-client-secret"
SCOPE = "https://graph.microsoft.com/.default"

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_USERS = f"{GRAPH_URL}/users"
GRAPH_GROUPS = f"{GRAPH_URL}/groups"


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
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class FakeClient:
    """Records every outbound request and returns canned responses.

    A single instance is returned for *every* ``await self._client()`` call so
    the app-token fetch in ``_app_auth_header`` and the subsequent Graph call
    are captured on the same recorder.
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

    async def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("DELETE", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._record(method, url, **kwargs)


def _adapter() -> AzureAdIdpAdapter:
    return AzureAdIdpAdapter(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allow_password_grant=True,
    )


@pytest.mark.asyncio
async def test_login_refused_without_password_grant_optin() -> None:
    """ROPC (grant_type=password) is refused unless explicitly enabled (RFC 9700 §2.4)."""
    from pyfly.kernel.exceptions import SecurityException

    adapter = AzureAdIdpAdapter(tenant_id=TENANT_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    with pytest.raises(SecurityException) as exc:
        await adapter.login(LoginRequest(username="alice@example.com", password="s3cr3t!"))
    assert exc.value.code == "ROPC_DISABLED"


def _inject(adapter: AzureAdIdpAdapter, fake: FakeClient) -> None:
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
# login — ROPC password grant → AuthResult (access/refresh/expires)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_password_grant_returns_authresult() -> None:
    """login() sends a ROPC password grant and parses the token response."""
    fake = FakeClient(
        [
            # The token endpoint is hit twice: once by login() and once by
            # find_by_username → get_user → _app_auth_header (client_credentials).
            # Both match "oauth2/v2.0/token" but login() fires first and the route
            # list is consumed in order with first-match, so we supply the same
            # stub for both and assert the password-grant shape separately.
            (
                "oauth2/v2.0/token",
                FakeResponse(
                    200,
                    json_body={
                        "access_token": "ACCESS-AAD",
                        "refresh_token": "REFRESH-AAD",
                        "expires_in": 3600,
                    },
                ),
            ),
            # find_by_username → get_user → GET /users/{username}
            (
                "/users/",
                FakeResponse(
                    200,
                    json_body={
                        "id": "aad-user-1",
                        "userPrincipalName": "alice@example.com",
                        "mail": "alice@example.com",
                        "givenName": "Alice",
                        "surname": "Smith",
                        "accountEnabled": True,
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.login(LoginRequest(username="alice@example.com", password="s3cr3t!"))

    # (a) outbound: password grant must carry credentials + correct grant_type
    login_req = _find(fake.requests, method="POST", needle="oauth2/v2.0/token")
    assert login_req["url"] == TOKEN_URL
    assert login_req["data"]["grant_type"] == "password"
    assert login_req["data"]["client_id"] == CLIENT_ID
    assert login_req["data"]["client_secret"] == CLIENT_SECRET
    assert login_req["data"]["username"] == "alice@example.com"
    assert login_req["data"]["password"] == "s3cr3t!"
    assert login_req["data"]["scope"] == SCOPE

    # (b) parsed: AuthResult must carry all three token fields
    assert result.access_token == "ACCESS-AAD"
    assert result.refresh_token == "REFRESH-AAD"
    assert result.expires_in == 3600

    # (c) parsed: user object must be populated from the Graph GET
    assert result.user.id == "aad-user-1"
    assert result.user.username == "alice@example.com"


@pytest.mark.asyncio
async def test_login_invalid_credentials_raises_permission_error() -> None:
    """login() raises PermissionError on any non-200 response from the token endpoint."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(401, json_body={"error": "invalid_grant"}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    with pytest.raises(PermissionError):
        await adapter.login(LoginRequest(username="alice@example.com", password="wrong"))

    # No Graph API calls should be attempted after the auth failure
    assert all("graph.microsoft.com" not in req["url"] for req in fake.requests)


# --------------------------------------------------------------------------- #
# get_user — Graph GET /users/{id} → parsed IdpUser
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_user_hits_graph_and_parses_idp_user() -> None:
    """get_user() fetches from Graph API and maps the AAD dict to IdpUser."""
    # First call will be _app_auth_header (client_credentials token),
    # second will be the GET /users/{id}.
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/users/",
                FakeResponse(
                    200,
                    json_body={
                        "id": "aad-user-42",
                        "userPrincipalName": "bob@example.com",
                        "mail": "bob@example.com",
                        "givenName": "Bob",
                        "surname": "Jones",
                        "accountEnabled": True,
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    user = await adapter.get_user("aad-user-42")

    # (a) outbound: app-token grant came first
    token_req = _find(fake.requests, method="POST", needle="oauth2/v2.0/token")
    assert token_req["data"]["grant_type"] == "client_credentials"

    # (a) outbound: user GET uses bearer header
    user_req = _find(fake.requests, method="GET", needle="/users/")
    assert "aad-user-42" in user_req["url"]
    assert user_req["headers"]["Authorization"] == "Bearer APP-TOKEN"

    # (b) parsed: all fields mapped from AAD response
    assert user is not None
    assert user.id == "aad-user-42"
    assert user.username == "bob@example.com"
    assert user.email == "bob@example.com"
    assert user.first_name == "Bob"
    assert user.last_name == "Jones"
    assert user.enabled is True


@pytest.mark.asyncio
async def test_get_user_returns_none_on_404() -> None:
    """get_user() returns None when the Graph API responds 404."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/users/",
                FakeResponse(404, json_body={}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.get_user("nonexistent-id")
    assert result is None


# --------------------------------------------------------------------------- #
# find_by_username — delegates to get_user (same Graph endpoint)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_by_username_delegates_to_get_user() -> None:
    """find_by_username() calls get_user() with the given username as the id."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/users/",
                FakeResponse(
                    200,
                    json_body={
                        "id": "aad-user-99",
                        "userPrincipalName": "carol@example.com",
                        "mail": "carol@example.com",
                        "givenName": "Carol",
                        "surname": "Lee",
                        "accountEnabled": True,
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    user = await adapter.find_by_username("carol@example.com")

    # (a) outbound: a GET /users/{username} was made
    user_req = _find(fake.requests, method="GET", needle="/users/")
    assert "carol@example.com" in user_req["url"]

    # (b) parsed: result is an IdpUser
    assert user is not None
    assert user.id == "aad-user-99"
    assert user.username == "carol@example.com"


# --------------------------------------------------------------------------- #
# assign_role / group membership — POST to /groups/{id}/members/$ref
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_assign_role_posts_to_group_members_ref() -> None:
    """assign_role() posts the user reference to the group members endpoint."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/members/$ref",
                FakeResponse(204, json_body={}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    ok = await adapter.assign_role("aad-user-42", "group-admin-id")

    # (a) outbound: POST to the group members $ref endpoint
    ref_req = _find(fake.requests, method="POST", needle="/members/$ref")
    assert "group-admin-id" in ref_req["url"]
    assert ref_req["headers"]["Authorization"] == "Bearer APP-TOKEN"
    assert ref_req["json"]["@odata.id"] == f"{GRAPH_URL}/directoryObjects/aad-user-42"

    # (b) parsed: 204 maps to True
    assert ok is True


@pytest.mark.asyncio
async def test_create_user_posts_to_graph_users_and_parses_id() -> None:
    """create_user() POSTs to /users with full profile and captures the returned id."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/users",
                FakeResponse(
                    201,
                    json_body={
                        "id": "new-aad-user-id",
                        "userPrincipalName": "dave@example.com",
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    new_user = IdpUser(username="dave", email="dave@example.com", first_name="Dave", last_name="Baker")
    result = await adapter.create_user(new_user, password="Str0ng!Pass")

    # (a) outbound: POST /users with profile fields
    create_req = _find(fake.requests, method="POST", needle="/users")
    assert create_req["url"] == GRAPH_USERS
    assert create_req["headers"]["Authorization"] == "Bearer APP-TOKEN"
    body = create_req["json"]
    assert body["mailNickname"] == "dave"
    assert body["userPrincipalName"] == "dave@example.com"
    assert body["givenName"] == "Dave"
    assert body["surname"] == "Baker"
    assert body["passwordProfile"]["password"] == "Str0ng!Pass"
    assert body["passwordProfile"]["forceChangePasswordNextSignIn"] is False

    # (b) parsed: id from the returned JSON
    assert result.id == "new-aad-user-id"


# --------------------------------------------------------------------------- #
# get_user_info — GET /me with delegated access token → IdpUser
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_user_info_calls_graph_me_with_delegated_token() -> None:
    """get_user_info() fetches /me from Graph using the supplied access token."""
    fake = FakeClient(
        [
            (
                "/me",
                FakeResponse(
                    200,
                    json_body={
                        "id": "aad-user-me",
                        "userPrincipalName": "eve@example.com",
                        "mail": "eve@example.com",
                        "givenName": "Eve",
                        "surname": "Chen",
                        "accountEnabled": True,
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    user = await adapter.get_user_info("DELEGATED-TOKEN")

    # (a) outbound: GET /me with the supplied delegated token (no app-token fetch)
    me_req = _find(fake.requests, method="GET", needle="/me")
    assert me_req["url"] == f"{GRAPH_URL}/me"
    assert me_req["headers"]["Authorization"] == "Bearer DELEGATED-TOKEN"

    # (b) parsed: IdpUser built from the /me response
    assert user is not None
    assert user.id == "aad-user-me"
    assert user.username == "eve@example.com"
    assert user.email == "eve@example.com"
    assert user.first_name == "Eve"
    assert user.last_name == "Chen"
    assert user.enabled is True


@pytest.mark.asyncio
async def test_get_user_info_returns_none_on_non_200() -> None:
    """get_user_info() returns None when Graph responds with a non-200 status."""
    fake = FakeClient(
        [
            (
                "/me",
                FakeResponse(401, json_body={"error": "InvalidAuthenticationToken"}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    result = await adapter.get_user_info("BAD-TOKEN")
    assert result is None


# --------------------------------------------------------------------------- #
# register_user — delegates to create_user (admin POST /users)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_register_user_forces_enabled_and_posts_to_graph() -> None:
    """register_user() sets enabled=True and delegates to create_user (POST /users)."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/users",
                FakeResponse(
                    201,
                    json_body={"id": "reg-user-id", "userPrincipalName": "grace@example.com"},
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    user = IdpUser(username="grace", email="grace@example.com", first_name="Grace", last_name="Wu", enabled=False)
    result = await adapter.register_user(user, password="Reg1st3r!")

    # (a) outbound: POST to /users (admin create path)
    create_req = _find(fake.requests, method="POST", needle="/users")
    assert create_req["url"] == GRAPH_USERS
    # enabled was forced True before the call
    assert create_req["json"]["accountEnabled"] is True

    # (b) parsed: id from the Graph response
    assert result.id == "reg-user-id"


# --------------------------------------------------------------------------- #
# get_roles — GET /users/{id}/memberOf → list[IdpRole]
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_roles_calls_member_of_and_parses_idp_roles() -> None:
    """get_roles() fetches /users/{id}/memberOf and maps each group to an IdpRole."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/memberOf",
                FakeResponse(
                    200,
                    json_body={
                        "value": [
                            {"id": "grp-admins", "displayName": "Admins"},
                            {"id": "grp-editors", "displayName": "Editors"},
                        ]
                    },
                ),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    roles = await adapter.get_roles("aad-user-42")

    # (a) outbound: GET /users/{id}/memberOf with app token
    member_req = _find(fake.requests, method="GET", needle="/memberOf")
    assert "aad-user-42" in member_req["url"]
    assert member_req["headers"]["Authorization"] == "Bearer APP-TOKEN"

    # (b) parsed: two IdpRole objects; name=group id, description=displayName
    assert len(roles) == 2
    names = {r.name for r in roles}
    assert names == {"grp-admins", "grp-editors"}
    assert all(isinstance(r, IdpRole) for r in roles)
    descriptions = {r.description for r in roles}
    assert "Admins" in descriptions
    assert "Editors" in descriptions


@pytest.mark.asyncio
async def test_get_roles_returns_empty_on_non_200() -> None:
    """get_roles() returns [] when the memberOf endpoint responds with non-200."""
    fake = FakeClient(
        [
            (
                "oauth2/v2.0/token",
                FakeResponse(200, json_body={"access_token": "APP-TOKEN", "expires_in": 300}),
            ),
            (
                "/memberOf",
                FakeResponse(404, json_body={}),
            ),
        ]
    )
    adapter = _adapter()
    _inject(adapter, fake)

    roles = await adapter.get_roles("nonexistent-user")
    assert roles == []
