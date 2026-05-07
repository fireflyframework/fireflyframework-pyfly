# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the IDP module."""

from __future__ import annotations

import pytest

from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter
from pyfly.idp.models import IdpUser, LoginRequest, PasswordChangeRequest


@pytest.mark.asyncio
async def test_create_login_logout() -> None:
    adapter = InternalDbIdpAdapter()
    user = await adapter.create_user(IdpUser(username="alice", email="a@x.com"), password="secret123")
    auth = await adapter.login(LoginRequest(username="alice", password="secret123"))
    assert auth.user.id == user.id
    assert auth.access_token

    introspection = await adapter.introspect(auth.access_token)
    assert introspection.active

    assert await adapter.logout(auth.access_token)
    assert not (await adapter.introspect(auth.access_token)).active


@pytest.mark.asyncio
async def test_login_failure() -> None:
    adapter = InternalDbIdpAdapter()
    await adapter.create_user(IdpUser(username="bob"), password="rightpass")
    with pytest.raises(PermissionError):
        await adapter.login(LoginRequest(username="bob", password="wrongpass"))


@pytest.mark.asyncio
async def test_change_password() -> None:
    adapter = InternalDbIdpAdapter()
    user = await adapter.create_user(IdpUser(username="charlie"), password="old-pw-1234")
    ok = await adapter.change_password(
        PasswordChangeRequest(user_id=user.id, old_password="old-pw-1234", new_password="new-pw-5678")
    )
    assert ok
    await adapter.login(LoginRequest(username="charlie", password="new-pw-5678"))


@pytest.mark.asyncio
async def test_role_management() -> None:
    adapter = InternalDbIdpAdapter()
    user = await adapter.create_user(IdpUser(username="dora"), password="pw-1234567")
    await adapter.assign_role(user.id, "admin")
    refreshed = await adapter.get_user(user.id)
    assert refreshed is not None and "admin" in refreshed.roles
    await adapter.revoke_role(user.id, "admin")
    refreshed = await adapter.get_user(user.id)
    assert refreshed is not None and "admin" not in refreshed.roles
