# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for MFA (TOTP) and extended IdpAdapter methods (Java parity, SP-7 Area B)."""

from __future__ import annotations

import pytest

from pyfly.idp.adapters.aws_cognito import AwsCognitoIdpAdapter
from pyfly.idp.adapters.azure_ad import AzureAdIdpAdapter
from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter
from pyfly.idp.adapters.keycloak import KeycloakIdpAdapter
from pyfly.idp.models import IdpUser, LoginRequest
from pyfly.idp.port import IdpAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _internal_adapter() -> InternalDbIdpAdapter:
    return InternalDbIdpAdapter()


# ---------------------------------------------------------------------------
# Protocol-compatibility — all 4 adapters must satisfy isinstance(x, IdpAdapter)
# ---------------------------------------------------------------------------


def test_protocol_compat_internal_db() -> None:
    assert isinstance(_internal_adapter(), IdpAdapter)


def test_protocol_compat_keycloak() -> None:
    adapter = KeycloakIdpAdapter(
        base_url="http://localhost:8080",
        realm="master",
        client_id="admin",
        client_secret="secret",
    )
    assert isinstance(adapter, IdpAdapter)


def test_protocol_compat_aws_cognito() -> None:
    adapter = AwsCognitoIdpAdapter(
        user_pool_id="us-east-1_Abc",
        client_id="client",
        region="us-east-1",
    )
    assert isinstance(adapter, IdpAdapter)


def test_protocol_compat_azure_ad() -> None:
    adapter = AzureAdIdpAdapter(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )
    assert isinstance(adapter, IdpAdapter)


# ---------------------------------------------------------------------------
# NotImplementedError guard — external adapters raise for MFA methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_mfa_challenge_raises() -> None:
    adapter = KeycloakIdpAdapter(
        base_url="http://localhost:8080",
        realm="master",
        client_id="admin",
        client_secret="secret",
    )
    with pytest.raises(NotImplementedError, match="Keycloak"):
        await adapter.mfa_challenge("some-user-id")


@pytest.mark.asyncio
async def test_keycloak_mfa_verify_raises() -> None:
    adapter = KeycloakIdpAdapter(
        base_url="http://localhost:8080",
        realm="master",
        client_id="admin",
        client_secret="secret",
    )
    with pytest.raises(NotImplementedError, match="Keycloak"):
        await adapter.mfa_verify("challenge-id", "123456")


@pytest.mark.asyncio
async def test_cognito_mfa_challenge_raises() -> None:
    adapter = AwsCognitoIdpAdapter(
        user_pool_id="us-east-1_Abc",
        client_id="client",
        region="us-east-1",
    )
    with pytest.raises(NotImplementedError, match="Cognito"):
        await adapter.mfa_challenge("some-user-id")


@pytest.mark.asyncio
async def test_cognito_mfa_verify_raises() -> None:
    adapter = AwsCognitoIdpAdapter(
        user_pool_id="us-east-1_Abc",
        client_id="client",
        region="us-east-1",
    )
    with pytest.raises(NotImplementedError, match="Cognito"):
        await adapter.mfa_verify("challenge-id", "123456")


@pytest.mark.asyncio
async def test_azure_mfa_challenge_raises() -> None:
    adapter = AzureAdIdpAdapter(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(NotImplementedError, match="Azure AD"):
        await adapter.mfa_challenge("some-user-id")


@pytest.mark.asyncio
async def test_azure_mfa_verify_raises() -> None:
    adapter = AzureAdIdpAdapter(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(NotImplementedError, match="Azure AD"):
        await adapter.mfa_verify("challenge-id", "123456")


# ---------------------------------------------------------------------------
# InternalDb — MFA flow (enable → login without code → verify → tokens)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mfa_enable_and_challenge_flow() -> None:
    import pyotp

    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="mfa_user", email="mfa@x.com"), password="pass1234!")

    # 1. Enable MFA — returns the provisioning secret.
    secret = await adapter.enable_mfa(user.id)
    assert len(secret) > 0

    # 2. Login WITHOUT mfa_code → mfa_required=True, no access token.
    result = await adapter.login(LoginRequest(username="mfa_user", password="pass1234!"))
    assert result.mfa_required is True
    assert result.access_token == ""
    assert result.mfa_challenge is not None
    challenge_id = result.mfa_challenge.challenge_id

    # 3. Verify with a VALID TOTP code → issues real tokens.
    valid_code = pyotp.TOTP(secret).now()
    auth = await adapter.mfa_verify(challenge_id, valid_code)
    assert auth.access_token != ""
    assert auth.mfa_required is False

    # 4. The issued token resolves via introspect.
    intro = await adapter.introspect(auth.access_token)
    assert intro.active
    assert intro.user_id == user.id


@pytest.mark.asyncio
async def test_mfa_verify_wrong_code_raises() -> None:

    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="mfa_bad", email="bad@x.com"), password="pass1234!")
    secret = await adapter.enable_mfa(user.id)
    assert secret  # keep reference for pyotp import side-effect

    result = await adapter.login(LoginRequest(username="mfa_bad", password="pass1234!"))
    challenge_id = result.mfa_challenge.challenge_id  # type: ignore[union-attr]

    with pytest.raises(PermissionError, match="invalid MFA code"):
        await adapter.mfa_verify(challenge_id, "000000")


@pytest.mark.asyncio
async def test_mfa_login_with_valid_inline_code() -> None:
    """login() with mfa_code supplied inline bypasses the challenge redirect."""
    import pyotp

    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="mfa_inline", email="inline@x.com"), password="pass1234!")
    secret = await adapter.enable_mfa(user.id)

    valid_code = pyotp.TOTP(secret).now()
    auth = await adapter.login(LoginRequest(username="mfa_inline", password="pass1234!", mfa_code=valid_code))
    assert auth.access_token != ""
    assert auth.mfa_required is False


@pytest.mark.asyncio
async def test_mfa_verify_expired_challenge_raises() -> None:
    """Verifying a challenge a second time (already consumed) raises PermissionError."""
    import pyotp

    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="mfa_exp", email="exp@x.com"), password="pass1234!")
    secret = await adapter.enable_mfa(user.id)

    result = await adapter.login(LoginRequest(username="mfa_exp", password="pass1234!"))
    challenge_id = result.mfa_challenge.challenge_id  # type: ignore[union-attr]

    valid_code = pyotp.TOTP(secret).now()
    await adapter.mfa_verify(challenge_id, valid_code)  # first use — ok

    with pytest.raises(PermissionError, match="invalid or expired"):
        await adapter.mfa_verify(challenge_id, valid_code)  # second use — consumed


# ---------------------------------------------------------------------------
# InternalDb — get_user_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_info_resolves_token() -> None:
    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="info_user"), password="pw123456!")
    auth = await adapter.login(LoginRequest(username="info_user", password="pw123456!"))

    resolved = await adapter.get_user_info(auth.access_token)
    assert resolved is not None
    assert resolved.id == user.id
    assert resolved.username == "info_user"


@pytest.mark.asyncio
async def test_get_user_info_unknown_token_returns_none() -> None:
    adapter = _internal_adapter()
    result = await adapter.get_user_info("totally-bogus-token")
    assert result is None


# ---------------------------------------------------------------------------
# InternalDb — register_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_user_always_enabled() -> None:
    adapter = _internal_adapter()
    new_user = IdpUser(username="reg_user", email="reg@x.com", enabled=False, roles=["admin"])
    registered = await adapter.register_user(new_user, password="reg-pass-1234!")

    # enabled must be forced True; admin role stripped.
    assert registered.enabled is True
    assert "admin" not in registered.roles

    # The user can log in immediately.
    auth = await adapter.login(LoginRequest(username="reg_user", password="reg-pass-1234!"))
    assert auth.access_token != ""


# ---------------------------------------------------------------------------
# InternalDb — get_roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_roles_returns_assigned_roles() -> None:
    adapter = _internal_adapter()
    user = await adapter.create_user(IdpUser(username="role_user"), password="pw123456!")
    await adapter.assign_role(user.id, "editor")
    await adapter.assign_role(user.id, "viewer")

    roles = await adapter.get_roles(user.id)
    role_names = {r.name for r in roles}
    assert role_names == {"editor", "viewer"}


@pytest.mark.asyncio
async def test_get_roles_unknown_user_returns_empty() -> None:
    adapter = _internal_adapter()
    roles = await adapter.get_roles("nonexistent-id")
    assert roles == []


@pytest.mark.asyncio
async def test_get_roles_with_catalogue_enriched_role() -> None:
    """Roles created via create_roles carry their description through get_roles."""
    adapter = _internal_adapter()
    await adapter.create_roles("superadmin")
    # Manually set description on the catalogue entry.
    adapter._roles["superadmin"].description = "Full access"  # noqa: SLF001
    user = await adapter.create_user(IdpUser(username="super_user"), password="pw123456!")
    await adapter.assign_role(user.id, "superadmin")

    roles = await adapter.get_roles(user.id)
    assert len(roles) == 1
    assert roles[0].name == "superadmin"
    assert roles[0].description == "Full access"
