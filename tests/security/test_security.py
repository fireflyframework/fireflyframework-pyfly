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
"""Tests for security module: @secure decorator, JWT, SecurityContext."""

import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.decorators import secure
from pyfly.security.jwt import JWTService


class TestSecurityContext:
    def test_anonymous_context(self):
        ctx = SecurityContext.anonymous()
        assert ctx.is_authenticated is False
        assert ctx.user_id is None
        assert ctx.roles == []
        assert ctx.permissions == []

    def test_authenticated_context(self):
        ctx = SecurityContext(
            user_id="user-1",
            roles=["ADMIN", "USER"],
            permissions=["order:read", "order:write"],
        )
        assert ctx.is_authenticated is True
        assert ctx.user_id == "user-1"
        assert ctx.has_role("ADMIN")
        assert ctx.has_role("USER")
        assert not ctx.has_role("SUPERADMIN")

    def test_has_permission(self):
        ctx = SecurityContext(
            user_id="user-1",
            roles=[],
            permissions=["order:read", "order:write"],
        )
        assert ctx.has_permission("order:read") is True
        assert ctx.has_permission("order:delete") is False

    def test_has_any_role(self):
        ctx = SecurityContext(user_id="user-1", roles=["USER"])
        assert ctx.has_any_role(["ADMIN", "USER"]) is True
        assert ctx.has_any_role(["ADMIN", "SUPERADMIN"]) is False


class TestSecureDecorator:
    @pytest.mark.asyncio
    async def test_allows_authorized_user(self):
        ctx = SecurityContext(user_id="admin-1", roles=["ADMIN"], permissions=["order:delete"])

        @secure(roles=["ADMIN"])
        async def delete_order(security_context: SecurityContext) -> str:
            return "deleted"

        result = await delete_order(security_context=ctx)
        assert result == "deleted"

    @pytest.mark.asyncio
    async def test_rejects_unauthorized_role(self):
        ctx = SecurityContext(user_id="user-1", roles=["USER"], permissions=[])

        @secure(roles=["ADMIN"])
        async def admin_only(security_context: SecurityContext) -> str:
            return "secret"

        with pytest.raises(SecurityException, match="Insufficient roles"):
            await admin_only(security_context=ctx)

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        ctx = SecurityContext.anonymous()

        @secure()
        async def protected(security_context: SecurityContext) -> str:
            return "data"

        with pytest.raises(SecurityException, match="Authentication required"):
            await protected(security_context=ctx)

    @pytest.mark.asyncio
    async def test_permission_check(self):
        ctx = SecurityContext(user_id="user-1", roles=["USER"], permissions=["order:read"])

        @secure(permissions=["order:delete"])
        async def delete_order(security_context: SecurityContext) -> str:
            return "deleted"

        with pytest.raises(SecurityException, match="Insufficient permissions"):
            await delete_order(security_context=ctx)

    @pytest.mark.asyncio
    async def test_allows_matching_permission(self):
        ctx = SecurityContext(user_id="user-1", roles=[], permissions=["order:delete"])

        @secure(permissions=["order:delete"])
        async def delete_order(security_context: SecurityContext) -> str:
            return "deleted"

        result = await delete_order(security_context=ctx)
        assert result == "deleted"


class TestJWTService:
    def test_encode_and_decode(self):
        jwt_service = JWTService(secret="test-secret-key-minimum-32-chars!")
        token = jwt_service.encode({"sub": "user-1", "roles": ["ADMIN"]})
        assert isinstance(token, str)

        payload = jwt_service.decode(token)
        assert payload["sub"] == "user-1"
        assert payload["roles"] == ["ADMIN"]

    def test_decode_invalid_token(self):
        jwt_service = JWTService(secret="test-secret-key-minimum-32-chars!")
        with pytest.raises(SecurityException, match="Invalid token"):
            jwt_service.decode("invalid.token.here")

    def test_to_security_context(self):
        jwt_service = JWTService(secret="test-secret-key-minimum-32-chars!")
        token = jwt_service.encode(
            {
                "sub": "user-1",
                "roles": ["ADMIN", "USER"],
                "permissions": ["order:read"],
            }
        )
        ctx = jwt_service.to_security_context(token)
        assert ctx.user_id == "user-1"
        assert ctx.has_role("ADMIN")
        assert ctx.has_permission("order:read")
