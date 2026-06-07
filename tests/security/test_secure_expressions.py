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
"""Tests for @secure decorator expression evaluation."""

from __future__ import annotations

import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.decorators import secure

# ---------------------------------------------------------------------------
# Decorated endpoint stubs
# ---------------------------------------------------------------------------


@secure(expression="hasRole('ADMIN')")
async def admin_only(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="hasAnyRole('ADMIN', 'MANAGER')")
async def admin_or_manager(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="hasPermission('user:read')")
async def read_users(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="isAuthenticated")
async def authenticated_only(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="hasRole('ADMIN') and hasPermission('user:write')")
async def admin_with_write(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="hasRole('ADMIN') or hasRole('USER')")
async def admin_or_user(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="not hasRole('GUEST')")
async def non_guest(security_context: SecurityContext) -> str:
    return "ok"


@secure(expression="(hasRole('ADMIN') or hasRole('MANAGER')) and hasPermission('write')")
async def complex_check(security_context: SecurityContext) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# hasRole tests
# ---------------------------------------------------------------------------


class TestHasRole:
    @pytest.mark.asyncio
    async def test_has_role_passes(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["ADMIN"])
        result = await admin_only(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_has_role_fails(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["USER"])
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await admin_only(security_context=ctx)


# ---------------------------------------------------------------------------
# hasAnyRole tests
# ---------------------------------------------------------------------------


class TestHasAnyRole:
    @pytest.mark.asyncio
    async def test_has_any_role_passes(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["MANAGER"])
        result = await admin_or_manager(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_has_any_role_fails(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["USER"])
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await admin_or_manager(security_context=ctx)


# ---------------------------------------------------------------------------
# hasPermission tests
# ---------------------------------------------------------------------------


class TestHasPermission:
    @pytest.mark.asyncio
    async def test_has_permission_passes(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=[], permissions=["user:read"])
        result = await read_users(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_has_permission_fails(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=[], permissions=["user:write"])
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await read_users(security_context=ctx)


# ---------------------------------------------------------------------------
# isAuthenticated tests
# ---------------------------------------------------------------------------


class TestIsAuthenticated:
    @pytest.mark.asyncio
    async def test_is_authenticated_passes(self) -> None:
        ctx = SecurityContext(user_id="user1")
        result = await authenticated_only(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_is_authenticated_fails(self) -> None:
        ctx = SecurityContext()  # anonymous -- user_id is None
        with pytest.raises(SecurityException, match="Authentication required"):
            await authenticated_only(security_context=ctx)


# ---------------------------------------------------------------------------
# Boolean operator tests
# ---------------------------------------------------------------------------


class TestBooleanOperators:
    @pytest.mark.asyncio
    async def test_and_expression_passes(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["ADMIN"],
            permissions=["user:write"],
        )
        result = await admin_with_write(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_and_expression_fails_missing_role(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["USER"],
            permissions=["user:write"],
        )
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await admin_with_write(security_context=ctx)

    @pytest.mark.asyncio
    async def test_and_expression_fails_missing_permission(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["ADMIN"],
            permissions=["user:read"],
        )
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await admin_with_write(security_context=ctx)

    @pytest.mark.asyncio
    async def test_or_expression_passes(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["USER"])
        result = await admin_or_user(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_or_expression_passes_other_branch(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["ADMIN"])
        result = await admin_or_user(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_or_expression_fails(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["GUEST"])
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await admin_or_user(security_context=ctx)

    @pytest.mark.asyncio
    async def test_not_expression_passes(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["USER"])
        result = await non_guest(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_not_expression_fails(self) -> None:
        ctx = SecurityContext(user_id="user1", roles=["GUEST"])
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await non_guest(security_context=ctx)


# ---------------------------------------------------------------------------
# Complex expression tests
# ---------------------------------------------------------------------------


class TestComplexExpression:
    @pytest.mark.asyncio
    async def test_complex_expression_admin_with_write(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["ADMIN"],
            permissions=["write"],
        )
        result = await complex_check(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_complex_expression_manager_with_write(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["MANAGER"],
            permissions=["write"],
        )
        result = await complex_check(security_context=ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_complex_expression_fails_no_write_permission(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["ADMIN"],
            permissions=["read"],
        )
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await complex_check(security_context=ctx)

    @pytest.mark.asyncio
    async def test_complex_expression_fails_wrong_role(self) -> None:
        ctx = SecurityContext(
            user_id="user1",
            roles=["USER"],
            permissions=["write"],
        )
        with pytest.raises(SecurityException, match="Access denied by expression"):
            await complex_check(security_context=ctx)


# ---------------------------------------------------------------------------
# Invalid expression tests
# ---------------------------------------------------------------------------


class TestInvalidExpression:
    @pytest.mark.asyncio
    async def test_invalid_expression_rejected(self) -> None:
        @secure(expression="__import__('os').system('echo hacked')")
        async def dangerous(security_context: SecurityContext) -> str:
            return "should not reach"

        ctx = SecurityContext(user_id="user1", roles=["ADMIN"])
        with pytest.raises(SecurityException, match="security expression"):
            await dangerous(security_context=ctx)

    @pytest.mark.asyncio
    async def test_arbitrary_python_rejected(self) -> None:
        @secure(expression="1 + 1 == 2")
        async def sneaky(security_context: SecurityContext) -> str:
            return "should not reach"

        ctx = SecurityContext(user_id="user1", roles=["ADMIN"])
        with pytest.raises(SecurityException, match="security expression"):
            await sneaky(security_context=ctx)
