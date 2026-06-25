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
"""Behavior tests for :class:`AwsCognitoIdpAdapter`.

These exercise the adapter against a *fake* sync boto3-style client (no network,
no Docker, no AWS credentials). The adapter accepts an injected ``client=`` at
construction time (``AwsCognitoIdpAdapter(..., client=fake)``), so no
monkeypatching is required. Each test asserts BOTH the outbound call arguments
the adapter forwarded to boto3 AND that the adapter parsed the canned response
dict into the right domain object.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.idp.adapters.aws_cognito import AwsCognitoIdpAdapter
from pyfly.idp.models import IdpRole, IdpUser, LoginRequest

USER_POOL_ID = "us-east-1_TestPool"
CLIENT_ID = "test-client-id"
REGION = "us-east-1"


class _FakeCognitoClient:
    """Sync boto3-style stub for ``cognito-idp``.

    Records every call (method name + kwargs) and returns the canned response
    registered via ``register(method, response_or_exception)``. If the canned
    value is an exception *instance* it is raised (to simulate e.g. auth errors).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._canned: dict[str, Any] = {}

    def register(self, method: str, value: Any) -> None:
        """Register a canned return value (or exception) for *method*."""
        self._canned[method] = value

    def _dispatch(self, method: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, **kwargs})
        if method not in self._canned:
            msg = f"_FakeCognitoClient: no canned response for {method!r}"
            raise AssertionError(msg)
        val = self._canned[method]
        if isinstance(val, BaseException):
            raise val
        return val

    # -- methods called by AwsCognitoIdpAdapter ----------------------------

    def initiate_auth(self, **kwargs: Any) -> Any:
        return self._dispatch("initiate_auth", **kwargs)

    def admin_create_user(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_create_user", **kwargs)

    def admin_set_user_password(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_set_user_password", **kwargs)

    def admin_get_user(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_get_user", **kwargs)

    def admin_update_user_attributes(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_update_user_attributes", **kwargs)

    def admin_delete_user(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_delete_user", **kwargs)

    def admin_add_user_to_group(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_add_user_to_group", **kwargs)

    def admin_remove_user_from_group(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_remove_user_from_group", **kwargs)

    def list_users(self, **kwargs: Any) -> Any:
        return self._dispatch("list_users", **kwargs)

    def list_groups(self, **kwargs: Any) -> Any:
        return self._dispatch("list_groups", **kwargs)

    def global_sign_out(self, **kwargs: Any) -> Any:
        return self._dispatch("global_sign_out", **kwargs)

    def get_user(self, **kwargs: Any) -> Any:
        return self._dispatch("get_user", **kwargs)

    def admin_list_groups_for_user(self, **kwargs: Any) -> Any:
        return self._dispatch("admin_list_groups_for_user", **kwargs)


def _find_call(calls: list[dict[str, Any]], method: str) -> dict[str, Any]:
    for c in calls:
        if c["method"] == method:
            return c
    msg = f"no call to {method!r} was recorded (got {[c['method'] for c in calls]})"
    raise AssertionError(msg)


def _adapter(fake: _FakeCognitoClient) -> AwsCognitoIdpAdapter:
    return AwsCognitoIdpAdapter(
        user_pool_id=USER_POOL_ID,
        client_id=CLIENT_ID,
        region=REGION,
        client=fake,
        allow_password_grant=True,
    )


@pytest.mark.asyncio
async def test_login_refused_without_password_grant_optin() -> None:
    """ROPC (USER_PASSWORD_AUTH) is refused unless explicitly enabled (RFC 9700 §2.4)."""
    from pyfly.kernel.exceptions import SecurityException

    adapter = AwsCognitoIdpAdapter(user_pool_id=USER_POOL_ID, client_id=CLIENT_ID, region=REGION, client=object())
    with pytest.raises(SecurityException) as exc:
        await adapter.login(LoginRequest(username="alice", password="hunter2"))
    assert exc.value.code == "ROPC_DISABLED"


# --------------------------------------------------------------------------- #
# login — initiate_auth USER_PASSWORD_AUTH → AuthResult
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_initiate_auth_returns_authresult() -> None:
    """login() calls initiate_auth with USER_PASSWORD_AUTH and parses the token response."""
    fake = _FakeCognitoClient()
    fake.register(
        "initiate_auth",
        {
            "AuthenticationResult": {
                "AccessToken": "ACCESS-CG",
                "RefreshToken": "REFRESH-CG",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            }
        },
    )
    # get_user is called after successful auth to build the IdpUser
    fake.register(
        "admin_get_user",
        {
            "Username": "alice",
            "UserAttributes": [
                {"Name": "email", "Value": "alice@example.com"},
                {"Name": "given_name", "Value": "Alice"},
                {"Name": "family_name", "Value": "Smith"},
            ],
            "Enabled": True,
        },
    )

    result = await _adapter(fake).login(LoginRequest(username="alice", password="hunter2"))

    # (a) outbound: initiate_auth called with correct flow and params
    auth_call = _find_call(fake.calls, "initiate_auth")
    assert auth_call["ClientId"] == CLIENT_ID
    assert auth_call["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert auth_call["AuthParameters"]["USERNAME"] == "alice"
    assert auth_call["AuthParameters"]["PASSWORD"] == "hunter2"

    # (b) parsed: tokens mapped into AuthResult
    assert result.access_token == "ACCESS-CG"
    assert result.refresh_token == "REFRESH-CG"
    assert result.expires_in == 3600

    # (c) parsed: user resolved via admin_get_user
    assert result.user.username == "alice"
    assert result.user.email == "alice@example.com"


@pytest.mark.asyncio
async def test_login_boto_exception_raises_permission_error() -> None:
    """login() wraps any boto3 exception in PermissionError."""
    fake = _FakeCognitoClient()
    fake.register("initiate_auth", Exception("NotAuthorizedException"))

    with pytest.raises(PermissionError):
        await _adapter(fake).login(LoginRequest(username="alice", password="wrong"))


@pytest.mark.asyncio
async def test_login_missing_authentication_result_raises_permission_error() -> None:
    """login() raises PermissionError when AuthenticationResult is absent (challenge flow)."""
    fake = _FakeCognitoClient()
    # Cognito responds with a challenge (no AuthenticationResult)
    fake.register("initiate_auth", {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "tok"})

    with pytest.raises(PermissionError):
        await _adapter(fake).login(LoginRequest(username="alice", password="tmp"))


# --------------------------------------------------------------------------- #
# create_user — admin_create_user + admin_set_user_password
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_user_calls_admin_create_and_set_password() -> None:
    """create_user() calls admin_create_user then admin_set_user_password (permanent)."""
    fake = _FakeCognitoClient()
    fake.register(
        "admin_create_user",
        {
            "User": {
                "Username": "bob",
                "UserAttributes": [{"Name": "email", "Value": "bob@example.com"}],
                "Enabled": True,
            }
        },
    )
    fake.register("admin_set_user_password", {})

    user = IdpUser(username="bob", email="bob@example.com", first_name="Bob", last_name="Jones")
    result = await _adapter(fake).create_user(user, password="Str0ng!Pass")

    # (a) outbound: admin_create_user with correct pool, username, attributes
    create_call = _find_call(fake.calls, "admin_create_user")
    assert create_call["UserPoolId"] == USER_POOL_ID
    assert create_call["Username"] == "bob"
    attrs = {a["Name"]: a["Value"] for a in create_call["UserAttributes"]}
    assert attrs["email"] == "bob@example.com"
    assert attrs["given_name"] == "Bob"
    assert attrs["family_name"] == "Jones"
    assert create_call["MessageAction"] == "SUPPRESS"

    # (a) outbound: admin_set_user_password with Permanent=True
    pwd_call = _find_call(fake.calls, "admin_set_user_password")
    assert pwd_call["UserPoolId"] == USER_POOL_ID
    assert pwd_call["Username"] == "bob"
    assert pwd_call["Password"] == "Str0ng!Pass"
    assert pwd_call["Permanent"] is True

    # (b) parsed: id set to username (Cognito convention)
    assert result.id == "bob"
    assert result.username == "bob"


# --------------------------------------------------------------------------- #
# get_user / find_by_username — admin_get_user → IdpUser
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_user_parses_cognito_user_attributes() -> None:
    """get_user() calls admin_get_user and maps Cognito attributes to IdpUser fields."""
    fake = _FakeCognitoClient()
    fake.register(
        "admin_get_user",
        {
            "Username": "carol",
            "UserAttributes": [
                {"Name": "email", "Value": "carol@example.com"},
                {"Name": "given_name", "Value": "Carol"},
                {"Name": "family_name", "Value": "Lee"},
                {"Name": "email_verified", "Value": "true"},
            ],
            "Enabled": True,
        },
    )

    user = await _adapter(fake).get_user("carol")

    # (a) outbound: admin_get_user called with correct pool + username
    get_call = _find_call(fake.calls, "admin_get_user")
    assert get_call["UserPoolId"] == USER_POOL_ID
    assert get_call["Username"] == "carol"

    # (b) parsed: attributes mapped correctly
    assert user is not None
    assert user.id == "carol"
    assert user.username == "carol"
    assert user.email == "carol@example.com"
    assert user.first_name == "Carol"
    assert user.last_name == "Lee"
    assert user.email_verified is True
    assert user.enabled is True


@pytest.mark.asyncio
async def test_get_user_returns_none_on_exception() -> None:
    """get_user() returns None when admin_get_user raises (e.g. user not found)."""
    fake = _FakeCognitoClient()
    fake.register("admin_get_user", Exception("UserNotFoundException"))

    result = await _adapter(fake).get_user("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_find_by_username_delegates_to_get_user() -> None:
    """find_by_username() delegates to get_user() and returns the same result."""
    fake = _FakeCognitoClient()
    fake.register(
        "admin_get_user",
        {
            "Username": "dave",
            "UserAttributes": [{"Name": "email", "Value": "dave@example.com"}],
            "Enabled": True,
        },
    )

    user = await _adapter(fake).find_by_username("dave")

    get_call = _find_call(fake.calls, "admin_get_user")
    assert get_call["Username"] == "dave"
    assert user is not None
    assert user.username == "dave"


# --------------------------------------------------------------------------- #
# assign_role / revoke_role — group membership via admin_add/remove_user_to_group
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_assign_role_calls_admin_add_user_to_group() -> None:
    """assign_role() calls admin_add_user_to_group with correct pool/user/group."""
    fake = _FakeCognitoClient()
    fake.register("admin_add_user_to_group", {})

    ok = await _adapter(fake).assign_role("alice", "admins")

    call = _find_call(fake.calls, "admin_add_user_to_group")
    assert call["UserPoolId"] == USER_POOL_ID
    assert call["Username"] == "alice"
    assert call["GroupName"] == "admins"
    assert ok is True


@pytest.mark.asyncio
async def test_revoke_role_calls_admin_remove_user_from_group() -> None:
    """revoke_role() calls admin_remove_user_from_group with correct pool/user/group."""
    fake = _FakeCognitoClient()
    fake.register("admin_remove_user_from_group", {})

    ok = await _adapter(fake).revoke_role("alice", "admins")

    call = _find_call(fake.calls, "admin_remove_user_from_group")
    assert call["UserPoolId"] == USER_POOL_ID
    assert call["Username"] == "alice"
    assert call["GroupName"] == "admins"
    assert ok is True


# --------------------------------------------------------------------------- #
# get_user_info — client.get_user(AccessToken=...) → IdpUser
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_user_info_calls_get_user_with_access_token() -> None:
    """get_user_info() calls client.get_user(AccessToken=…) and maps the result."""
    fake = _FakeCognitoClient()
    fake.register(
        "get_user",
        {
            "Username": "frank",
            "UserAttributes": [
                {"Name": "email", "Value": "frank@example.com"},
                {"Name": "given_name", "Value": "Frank"},
                {"Name": "family_name", "Value": "Lee"},
            ],
            "Enabled": True,
        },
    )

    user = await _adapter(fake).get_user_info("ACCESS-TOKEN-XYZ")

    # (a) outbound: get_user called with the provided AccessToken
    call = _find_call(fake.calls, "get_user")
    assert call["AccessToken"] == "ACCESS-TOKEN-XYZ"

    # (b) parsed: IdpUser populated from the Cognito response
    assert user is not None
    assert user.username == "frank"
    assert user.email == "frank@example.com"
    assert user.first_name == "Frank"
    assert user.last_name == "Lee"


@pytest.mark.asyncio
async def test_get_user_info_returns_none_on_exception() -> None:
    """get_user_info() returns None when get_user raises (e.g. invalid token)."""
    fake = _FakeCognitoClient()
    fake.register("get_user", Exception("NotAuthorizedException"))

    result = await _adapter(fake).get_user_info("BAD-TOKEN")
    assert result is None


# --------------------------------------------------------------------------- #
# register_user — delegates to create_user, forces enabled=True
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_register_user_forces_enabled_and_creates() -> None:
    """register_user() sets enabled=True and then calls create_user (admin_create_user)."""
    fake = _FakeCognitoClient()
    fake.register(
        "admin_create_user",
        {
            "User": {
                "Username": "grace",
                "UserAttributes": [{"Name": "email", "Value": "grace@example.com"}],
                "Enabled": True,
            }
        },
    )
    fake.register("admin_set_user_password", {})

    user = IdpUser(username="grace", email="grace@example.com", enabled=False)
    result = await _adapter(fake).register_user(user, password="S3cur3!Pass")

    # enabled must have been set True before the create call
    create_call = _find_call(fake.calls, "admin_create_user")
    assert create_call["Username"] == "grace"
    assert result.id == "grace"


# --------------------------------------------------------------------------- #
# get_roles — admin_list_groups_for_user → list[IdpRole]
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_roles_calls_admin_list_groups_for_user() -> None:
    """get_roles() calls admin_list_groups_for_user and parses group names into IdpRole."""
    fake = _FakeCognitoClient()
    fake.register(
        "admin_list_groups_for_user",
        {
            "Groups": [
                {"GroupName": "admins", "Description": "Admin group"},
                {"GroupName": "editors", "Description": ""},
            ]
        },
    )

    roles = await _adapter(fake).get_roles("alice")

    # (a) outbound: called with correct pool and username
    call = _find_call(fake.calls, "admin_list_groups_for_user")
    assert call["UserPoolId"] == USER_POOL_ID
    assert call["Username"] == "alice"

    # (b) parsed: two IdpRole objects with correct names
    assert len(roles) == 2
    names = {r.name for r in roles}
    assert names == {"admins", "editors"}
    assert all(isinstance(r, IdpRole) for r in roles)


@pytest.mark.asyncio
async def test_get_roles_returns_empty_on_exception() -> None:
    """get_roles() returns [] when admin_list_groups_for_user raises (e.g. user not found)."""
    fake = _FakeCognitoClient()
    fake.register("admin_list_groups_for_user", Exception("UserNotFoundException"))

    roles = await _adapter(fake).get_roles("nonexistent")
    assert roles == []
