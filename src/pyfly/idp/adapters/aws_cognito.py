# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""AWS Cognito IDP adapter — wraps boto3's CognitoIdentityProvider client."""

from __future__ import annotations

import asyncio
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


class AwsCognitoIdpAdapter:
    """Bridge to AWS Cognito User Pool via boto3.

    Args:
        user_pool_id: e.g. ``us-east-1_AbcDef``.
        client_id: app client id.
        region: AWS region.
    """

    name = "aws-cognito"

    def __init__(
        self,
        *,
        user_pool_id: str,
        client_id: str,
        region: str,
        client: Any | None = None,
    ) -> None:
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._region = region
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # noqa: BLE001
            msg = "AwsCognitoIdpAdapter requires boto3 — `pip install boto3`"
            raise ImportError(msg) from exc
        self._client = boto3.client("cognito-idp", region_name=self._region)
        return self._client

    async def _run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def create_user(self, user: IdpUser, password: str) -> IdpUser:
        client = self._ensure_client()
        attributes = [{"Name": "email", "Value": user.email}] if user.email else []
        if user.first_name:
            attributes.append({"Name": "given_name", "Value": user.first_name})
        if user.last_name:
            attributes.append({"Name": "family_name", "Value": user.last_name})
        await self._run(
            client.admin_create_user,
            UserPoolId=self._user_pool_id,
            Username=user.username,
            UserAttributes=attributes,
            TemporaryPassword=password,
            MessageAction="SUPPRESS",
        )
        await self._run(
            client.admin_set_user_password,
            UserPoolId=self._user_pool_id,
            Username=user.username,
            Password=password,
            Permanent=True,
        )
        user.id = user.username
        return user

    async def get_user(self, user_id: str) -> IdpUser | None:
        client = self._ensure_client()
        try:
            data = await self._run(
                client.admin_get_user, UserPoolId=self._user_pool_id, Username=user_id
            )
        except Exception:  # noqa: BLE001
            return None
        return _from_cognito(data)

    async def find_by_username(self, username: str) -> IdpUser | None:
        return await self.get_user(username)

    async def update_user(self, user: IdpUser) -> IdpUser:
        client = self._ensure_client()
        attrs = []
        if user.email:
            attrs.append({"Name": "email", "Value": user.email})
        if user.first_name:
            attrs.append({"Name": "given_name", "Value": user.first_name})
        if user.last_name:
            attrs.append({"Name": "family_name", "Value": user.last_name})
        if attrs:
            await self._run(
                client.admin_update_user_attributes,
                UserPoolId=self._user_pool_id,
                Username=user.id or user.username,
                UserAttributes=attrs,
            )
        return user

    async def delete_user(self, user_id: str) -> bool:
        client = self._ensure_client()
        try:
            await self._run(client.admin_delete_user, UserPoolId=self._user_pool_id, Username=user_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def list_users(self, *, limit: int = 100) -> list[IdpUser]:
        client = self._ensure_client()
        data = await self._run(client.list_users, UserPoolId=self._user_pool_id, Limit=limit)
        return [_from_cognito(u) for u in data.get("Users", [])]

    async def login(self, request: LoginRequest) -> AuthResult:
        client = self._ensure_client()
        try:
            resp = await self._run(
                client.initiate_auth,
                ClientId=self._client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": request.username, "PASSWORD": request.password},
            )
        except Exception as exc:  # noqa: BLE001
            msg = "invalid credentials"
            raise PermissionError(msg) from exc
        result = resp["AuthenticationResult"]
        user = await self.get_user(request.username) or IdpUser(username=request.username)
        return AuthResult(
            user=user,
            access_token=result["AccessToken"],
            refresh_token=result.get("RefreshToken"),
            expires_in=result.get("ExpiresIn", 3600),
        )

    async def logout(self, access_token: str) -> bool:
        client = self._ensure_client()
        try:
            await self._run(client.global_sign_out, AccessToken=access_token)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def refresh(self, refresh_token: str) -> AuthResult:
        client = self._ensure_client()
        resp = await self._run(
            client.initiate_auth,
            ClientId=self._client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
        result = resp["AuthenticationResult"]
        return AuthResult(
            user=IdpUser(),
            access_token=result["AccessToken"],
            refresh_token=refresh_token,
            expires_in=result.get("ExpiresIn", 3600),
        )

    async def introspect(self, access_token: str) -> SessionIntrospection:
        client = self._ensure_client()
        try:
            data = await self._run(client.get_user, AccessToken=access_token)
        except Exception:  # noqa: BLE001
            return SessionIntrospection(active=False)
        return SessionIntrospection(
            active=True,
            user_id=data.get("Username"),
            username=data.get("Username"),
        )

    async def change_password(self, request: PasswordChangeRequest) -> bool:
        client = self._ensure_client()
        try:
            await self._run(
                client.admin_set_user_password,
                UserPoolId=self._user_pool_id,
                Username=request.user_id,
                Password=request.new_password,
                Permanent=True,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def reset_password(self, user_id: str) -> str:
        import secrets

        new_password = secrets.token_urlsafe(16)
        await self.change_password(
            PasswordChangeRequest(user_id=user_id, old_password="", new_password=new_password)
        )
        return new_password

    async def assign_role(self, user_id: str, role: str) -> bool:
        client = self._ensure_client()
        try:
            await self._run(
                client.admin_add_user_to_group,
                UserPoolId=self._user_pool_id,
                Username=user_id,
                GroupName=role,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def revoke_role(self, user_id: str, role: str) -> bool:
        client = self._ensure_client()
        try:
            await self._run(
                client.admin_remove_user_from_group,
                UserPoolId=self._user_pool_id,
                Username=user_id,
                GroupName=role,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def list_roles(self) -> list[IdpRole]:
        client = self._ensure_client()
        data = await self._run(client.list_groups, UserPoolId=self._user_pool_id)
        return [IdpRole(name=g["GroupName"], description=g.get("Description", "")) for g in data.get("Groups", [])]


def _from_cognito(data: dict[str, Any]) -> IdpUser:
    attrs = {a["Name"]: a["Value"] for a in data.get("UserAttributes") or data.get("Attributes") or []}
    return IdpUser(
        id=data.get("Username", ""),
        username=data.get("Username", ""),
        email=attrs.get("email", ""),
        first_name=attrs.get("given_name", ""),
        last_name=attrs.get("family_name", ""),
        enabled=data.get("Enabled", True),
        email_verified=attrs.get("email_verified") == "true",
        attributes=attrs,
    )
