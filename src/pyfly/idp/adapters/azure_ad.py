# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Azure AD / Entra ID IDP adapter — talks to the Microsoft Graph API."""

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


class AzureAdIdpAdapter:
    """Bridge to Azure AD / Microsoft Entra via Graph + token endpoints.

    Args:
        tenant_id: directory id.
        client_id: registered app id.
        client_secret: registered app secret.
        scope: token scope (default ``https://graph.microsoft.com/.default``).
    """

    name = "azure-ad"

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str = "https://graph.microsoft.com/.default",
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._app_token: str | None = None

    @property
    def _graph(self) -> str:
        return "https://graph.microsoft.com/v1.0"

    @property
    def _token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "AzureAdIdpAdapter requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(timeout=30.0)

    async def _app_auth_header(self) -> dict[str, str]:
        if self._app_token is None:
            async with await self._client() as client:
                resp = await client.post(
                    self._token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": self._scope,
                    },
                )
                resp.raise_for_status()
                self._app_token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._app_token}"}

    async def create_user(self, user: IdpUser, password: str) -> IdpUser:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.post(
                f"{self._graph}/users",
                json={
                    "accountEnabled": user.enabled,
                    "displayName": f"{user.first_name} {user.last_name}".strip() or user.username,
                    "mailNickname": user.username,
                    "userPrincipalName": user.email or user.username,
                    "givenName": user.first_name,
                    "surname": user.last_name,
                    "passwordProfile": {
                        "forceChangePasswordNextSignIn": False,
                        "password": password,
                    },
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            user.id = data["id"]
        return user

    async def get_user(self, user_id: str) -> IdpUser | None:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.get(f"{self._graph}/users/{user_id}", headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return _from_aad(resp.json())

    async def find_by_username(self, username: str) -> IdpUser | None:
        return await self.get_user(username)

    async def update_user(self, user: IdpUser) -> IdpUser:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            payload = {
                "accountEnabled": user.enabled,
                "givenName": user.first_name,
                "surname": user.last_name,
            }
            await client.patch(f"{self._graph}/users/{user.id}", json=payload, headers=headers)
        return user

    async def delete_user(self, user_id: str) -> bool:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.delete(f"{self._graph}/users/{user_id}", headers=headers)
            return resp.status_code in (200, 204)

    async def list_users(self, *, limit: int = 100) -> list[IdpUser]:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.get(f"{self._graph}/users", params={"$top": limit}, headers=headers)
            resp.raise_for_status()
            return [_from_aad(u) for u in resp.json().get("value", [])]

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
                    "scope": self._scope,
                },
            )
            if resp.status_code != 200:
                msg = "invalid credentials"
                raise PermissionError(msg)
            tokens = resp.json()
            user = await self.find_by_username(request.username) or IdpUser(username=request.username)
            return AuthResult(
                user=user,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                expires_in=tokens.get("expires_in", 3600),
            )

    async def logout(self, access_token: str) -> bool:
        # Azure AD does not have a server-side logout for non-interactive clients.
        return True

    async def refresh(self, refresh_token: str) -> AuthResult:
        async with await self._client() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "scope": self._scope,
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
            resp = await client.get(
                f"{self._graph}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                return SessionIntrospection(active=False)
            data = resp.json()
            return SessionIntrospection(
                active=True,
                user_id=data.get("id"),
                username=data.get("userPrincipalName"),
            )

    async def change_password(self, request: PasswordChangeRequest) -> bool:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.patch(
                f"{self._graph}/users/{request.user_id}",
                json={
                    "passwordProfile": {
                        "forceChangePasswordNextSignIn": False,
                        "password": request.new_password,
                    }
                },
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def reset_password(self, user_id: str) -> str:
        import secrets

        new_password = secrets.token_urlsafe(16)
        await self.change_password(PasswordChangeRequest(user_id=user_id, old_password="", new_password=new_password))
        return new_password

    async def assign_role(self, user_id: str, role: str) -> bool:
        # Roles in Azure AD are typically Group memberships.
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.post(
                f"{self._graph}/groups/{role}/members/$ref",
                json={"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"},
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def revoke_role(self, user_id: str, role: str) -> bool:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.delete(
                f"{self._graph}/groups/{role}/members/{user_id}/$ref",
                headers=headers,
            )
            return resp.status_code in (200, 204)

    async def list_roles(self) -> list[IdpRole]:
        async with await self._client() as client:
            headers = await self._app_auth_header()
            resp = await client.get(f"{self._graph}/groups", headers=headers)
            resp.raise_for_status()
            return [IdpRole(name=g["id"], description=g.get("displayName", "")) for g in resp.json().get("value", [])]


def _from_aad(data: dict[str, Any]) -> IdpUser:
    return IdpUser(
        id=data.get("id", ""),
        username=data.get("userPrincipalName", ""),
        email=data.get("mail") or data.get("userPrincipalName", ""),
        enabled=data.get("accountEnabled", True),
        first_name=data.get("givenName", "") or "",
        last_name=data.get("surname", "") or "",
    )
