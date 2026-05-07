# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Keycloak IDP adapter — talks to a Keycloak server's REST API.

Designed to work with the Admin API (``/admin/realms/{realm}/users``) and the
token endpoint (``/realms/{realm}/protocol/openid-connect/token``).

The adapter relies only on ``httpx`` (already a pyfly dep when the ``client``
extra is installed); when ``httpx`` is missing, every method raises
``ImportError`` so users know to install it.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.idp.models import (
    AuthResult,
    IdpRole,
    IdpUser,
    LoginRequest,
    PasswordChangeRequest,
    SessionIntrospection,
)

_logger = logging.getLogger(__name__)


class KeycloakIdpAdapter:
    """Bridge to a Keycloak realm via its REST API.

    Args:
        base_url: e.g. ``https://keycloak.example.com``.
        realm: realm name.
        client_id: confidential client id used for the admin grant.
        client_secret: client secret.
        verify_ssl: whether to verify TLS certificates (default ``True``).
    """

    name = "keycloak"

    def __init__(
        self,
        *,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        verify_ssl: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._realm = realm
        self._client_id = client_id
        self._client_secret = client_secret
        self._verify = verify_ssl
        self._admin_token: str | None = None

    # -- helpers -----------------------------------------------------------

    @property
    def _admin_path(self) -> str:
        return f"{self._base_url}/admin/realms/{self._realm}"

    @property
    def _token_url(self) -> str:
        return f"{self._base_url}/realms/{self._realm}/protocol/openid-connect/token"

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # noqa: BLE001
            msg = "KeycloakIdpAdapter requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(verify=self._verify, timeout=30.0)

    async def _admin_auth_header(self) -> dict[str, str]:
        if self._admin_token is None:
            async with await self._client() as client:
                resp = await client.post(
                    self._token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
                resp.raise_for_status()
                self._admin_token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._admin_token}"}

    # -- IdpAdapter protocol ----------------------------------------------

    async def create_user(self, user: IdpUser, password: str) -> IdpUser:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            payload = {
                "username": user.username,
                "email": user.email,
                "enabled": user.enabled,
                "emailVerified": user.email_verified,
                "firstName": user.first_name,
                "lastName": user.last_name,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
                "attributes": user.attributes,
            }
            resp = await client.post(f"{self._admin_path}/users", json=payload, headers=headers)
            resp.raise_for_status()
            location = resp.headers.get("Location", "")
            user.id = location.rsplit("/", 1)[-1] or user.id
        return user

    async def get_user(self, user_id: str) -> IdpUser | None:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.get(f"{self._admin_path}/users/{user_id}", headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return _from_kc(resp.json())

    async def find_by_username(self, username: str) -> IdpUser | None:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.get(
                f"{self._admin_path}/users", params={"username": username, "exact": "true"}, headers=headers
            )
            resp.raise_for_status()
            arr = resp.json()
            return _from_kc(arr[0]) if arr else None

    async def update_user(self, user: IdpUser) -> IdpUser:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.put(
                f"{self._admin_path}/users/{user.id}",
                json={
                    "email": user.email,
                    "enabled": user.enabled,
                    "emailVerified": user.email_verified,
                    "firstName": user.first_name,
                    "lastName": user.last_name,
                    "attributes": user.attributes,
                },
                headers=headers,
            )
            resp.raise_for_status()
        return user

    async def delete_user(self, user_id: str) -> bool:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.delete(f"{self._admin_path}/users/{user_id}", headers=headers)
            return resp.status_code in (200, 204)

    async def list_users(self, *, limit: int = 100) -> list[IdpUser]:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.get(f"{self._admin_path}/users", params={"max": limit}, headers=headers)
            resp.raise_for_status()
            return [_from_kc(u) for u in resp.json()]

    async def login(self, request: LoginRequest) -> AuthResult:
        async with await self._client() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "password",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "username": request.username,
                    "password": request.password,
                },
            )
            if resp.status_code != 200:
                msg = "invalid credentials"
                raise PermissionError(msg)
            tokens = resp.json()
            user = await self.find_by_username(request.username)
            if user is None:
                user = IdpUser(username=request.username)
            return AuthResult(
                user=user,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                expires_in=tokens.get("expires_in", 3600),
            )

    async def logout(self, access_token: str) -> bool:
        async with await self._client() as client:
            resp = await client.post(
                f"{self._base_url}/realms/{self._realm}/protocol/openid-connect/logout",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            return resp.status_code in (200, 204)

    async def refresh(self, refresh_token: str) -> AuthResult:
        async with await self._client() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                },
            )
            resp.raise_for_status()
            tokens = resp.json()
            return AuthResult(
                user=IdpUser(),
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token", refresh_token),
                expires_in=tokens.get("expires_in", 3600),
            )

    async def introspect(self, access_token: str) -> SessionIntrospection:
        async with await self._client() as client:
            resp = await client.post(
                f"{self._base_url}/realms/{self._realm}/protocol/openid-connect/token/introspect",
                data={"client_id": self._client_id, "client_secret": self._client_secret, "token": access_token},
            )
            resp.raise_for_status()
            data = resp.json()
            return SessionIntrospection(
                active=bool(data.get("active")),
                user_id=data.get("sub"),
                username=data.get("preferred_username"),
                scopes=(data.get("scope") or "").split(),
            )

    async def change_password(self, request: PasswordChangeRequest) -> bool:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.put(
                f"{self._admin_path}/users/{request.user_id}/reset-password",
                json={"type": "password", "value": request.new_password, "temporary": False},
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def reset_password(self, user_id: str) -> str:
        import secrets

        new_password = secrets.token_urlsafe(16)
        await self.change_password(
            PasswordChangeRequest(user_id=user_id, old_password="", new_password=new_password)
        )
        return new_password

    async def assign_role(self, user_id: str, role: str) -> bool:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            roles = await client.get(f"{self._admin_path}/roles/{role}", headers=headers)
            if roles.status_code != 200:
                return False
            resp = await client.post(
                f"{self._admin_path}/users/{user_id}/role-mappings/realm",
                json=[roles.json()],
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def revoke_role(self, user_id: str, role: str) -> bool:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            roles = await client.get(f"{self._admin_path}/roles/{role}", headers=headers)
            if roles.status_code != 200:
                return False
            resp = await client.request(
                "DELETE",
                f"{self._admin_path}/users/{user_id}/role-mappings/realm",
                json=[roles.json()],
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def list_roles(self) -> list[IdpRole]:
        async with await self._client() as client:
            headers = await self._admin_auth_header()
            resp = await client.get(f"{self._admin_path}/roles", headers=headers)
            resp.raise_for_status()
            return [IdpRole(name=r["name"], description=r.get("description", "")) for r in resp.json()]


def _from_kc(data: dict[str, Any]) -> IdpUser:
    return IdpUser(
        id=data.get("id", ""),
        username=data.get("username", ""),
        email=data.get("email", ""),
        enabled=data.get("enabled", True),
        email_verified=data.get("emailVerified", False),
        first_name=data.get("firstName", ""),
        last_name=data.get("lastName", ""),
        attributes=data.get("attributes", {}),
    )
