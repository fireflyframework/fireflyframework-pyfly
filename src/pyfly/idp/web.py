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
"""IDP REST controller — exposes the IdpAdapter over HTTP (audit #22).

Mirrors the Java ``IdpController`` (@RequestMapping("/idp")): authentication
(/login, /refresh, /logout, /introspect) plus admin user/role management. The
controller is registered as a bean by :class:`IdpWebAutoConfiguration` and
mounted by the post-start route rescan in ``create_app``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, cast

from pydantic import BaseModel

from pyfly.container import rest_controller
from pyfly.idp.models import IdpUser, LoginRequest
from pyfly.idp.port import IdpAdapter
from pyfly.web import Body, PathVar, QueryParam, Valid, delete_mapping, get_mapping, post_mapping, request_mapping


class LoginBody(BaseModel):
    username: str
    password: str
    mfa_code: str | None = None


class TokenBody(BaseModel):
    token: str


class CreateUserBody(BaseModel):
    username: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    password: str
    roles: list[str] = []


def _to_dict(obj: Any) -> Any:
    """Convert a dataclass result to a JSON-safe dict (datetimes → ISO strings)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json.loads(json.dumps(dataclasses.asdict(obj), default=str))
    return obj


@rest_controller
@request_mapping("/idp")
class IdpController:
    """``/idp`` — authentication + admin endpoints backed by the IdpAdapter."""

    base_path = "/idp"

    def __init__(self, idp_adapter: IdpAdapter) -> None:
        self._idp = idp_adapter

    # -- Authentication -----------------------------------------------------

    @post_mapping("/login")
    async def login(self, body: Valid[Body[LoginBody]]) -> dict[str, Any]:
        req = body
        result = await self._idp.login(
            LoginRequest(username=req.username, password=req.password, mfa_code=req.mfa_code)
        )
        return cast(dict[str, Any], _to_dict(result))

    @post_mapping("/refresh")
    async def refresh(self, body: Valid[Body[TokenBody]]) -> dict[str, Any]:
        result = await self._idp.refresh(body.token)
        return cast(dict[str, Any], _to_dict(result))

    @post_mapping("/logout")
    async def logout(self, body: Valid[Body[TokenBody]]) -> dict[str, bool]:
        ok = await self._idp.logout(body.token)
        return {"success": ok}

    @post_mapping("/introspect")
    async def introspect(self, body: Valid[Body[TokenBody]]) -> dict[str, Any]:
        result = await self._idp.introspect(body.token)
        return cast(dict[str, Any], _to_dict(result))

    # -- Admin: users -------------------------------------------------------

    @post_mapping("/admin/users")
    async def create_user(self, body: Valid[Body[CreateUserBody]]) -> dict[str, Any]:
        req = body
        user = IdpUser(
            username=req.username,
            email=req.email,
            first_name=req.first_name,
            last_name=req.last_name,
            roles=list(req.roles),
        )
        created = await self._idp.create_user(user, req.password)
        return cast(dict[str, Any], _to_dict(created))

    @get_mapping("/admin/users/{user_id}")
    async def get_user(self, user_id: PathVar[str]) -> dict[str, Any] | None:
        user = await self._idp.get_user(user_id)
        return _to_dict(user) if user is not None else None

    @get_mapping("/admin/users")
    async def list_users(self, limit: QueryParam[int] = 100) -> list[dict[str, Any]]:
        users = await self._idp.list_users(limit=int(limit))
        return [cast(dict[str, Any], _to_dict(u)) for u in users]

    @delete_mapping("/admin/users/{user_id}")
    async def delete_user(self, user_id: PathVar[str]) -> dict[str, bool]:
        ok = await self._idp.delete_user(user_id)
        return {"success": ok}

    # -- Admin: roles -------------------------------------------------------

    @post_mapping("/admin/users/{user_id}/roles/{role}")
    async def assign_role(self, user_id: PathVar[str], role: PathVar[str]) -> dict[str, bool]:
        ok = await self._idp.assign_role(user_id, role)
        return {"success": ok}

    @delete_mapping("/admin/users/{user_id}/roles/{role}")
    async def revoke_role(self, user_id: PathVar[str], role: PathVar[str]) -> dict[str, bool]:
        ok = await self._idp.revoke_role(user_id, role)
        return {"success": ok}

    @get_mapping("/admin/roles")
    async def list_roles(self) -> list[dict[str, Any]]:
        roles = await self._idp.list_roles()
        return [cast(dict[str, Any], _to_dict(r)) for r in roles]
