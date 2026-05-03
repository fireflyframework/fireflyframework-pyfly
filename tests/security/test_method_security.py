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
"""Tests for @pre_authorize and @post_authorize method-level security decorators."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.context.request_context import RequestContext
from pyfly.kernel.exceptions import ForbiddenException, UnauthorizedException
from pyfly.security.context import SecurityContext
from pyfly.security.method_security import post_authorize, pre_authorize

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_request_context() -> Any:
    """Ensure RequestContext is clean before and after each test."""
    RequestContext.clear()
    yield
    RequestContext.clear()


# ---------------------------------------------------------------------------
# Decorated function stubs
# ---------------------------------------------------------------------------


@pre_authorize("isAuthenticated")
async def async_authenticated_only() -> str:
    return "ok"


@pre_authorize("hasRole('ADMIN')")
async def async_admin_only() -> str:
    return "ok"


@pre_authorize("hasRole('ADMIN') or hasPermission('order:write')")
async def async_admin_or_order_write() -> str:
    return "ok"


@post_authorize("hasPermission('order:read')")
async def async_post_order_read() -> str:
    return "ok"


side_effect_tracker: list[str] = []


@post_authorize("hasPermission('secret:read')")
async def async_post_with_side_effect() -> str:
    side_effect_tracker.append("executed")
    return "result"


@pre_authorize("isAuthenticated")
def sync_authenticated_only() -> str:
    return "sync_ok"


@pre_authorize("hasRole('ADMIN')")
def sync_admin_only() -> str:
    return "sync_ok"


@post_authorize("hasPermission('order:read')")
def sync_post_order_read() -> str:
    return "sync_ok"


sync_side_effect_tracker: list[str] = []


@post_authorize("hasPermission('secret:read')")
def sync_post_with_side_effect() -> str:
    sync_side_effect_tracker.append("executed")
    return "sync_result"


# ---------------------------------------------------------------------------
# @pre_authorize - async tests
# ---------------------------------------------------------------------------


class TestPreAuthorizeAsync:
    @pytest.mark.asyncio
    async def test_is_authenticated_allows_authenticated_user(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1")
        result = await async_authenticated_only()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_is_authenticated_raises_when_no_security_context(self) -> None:
        RequestContext.init()  # no security_context set
        with pytest.raises(UnauthorizedException, match="Authentication required"):
            await async_authenticated_only()

    @pytest.mark.asyncio
    async def test_is_authenticated_raises_when_no_request_context(self) -> None:
        with pytest.raises(UnauthorizedException, match="Authentication required"):
            await async_authenticated_only()

    @pytest.mark.asyncio
    async def test_has_role_allows_matching_role(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1", roles=["ADMIN"])
        result = await async_admin_only()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_has_role_raises_forbidden_for_wrong_role(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1", roles=["USER"])
        with pytest.raises(ForbiddenException, match="Access denied by expression"):
            await async_admin_only()

    @pytest.mark.asyncio
    async def test_compound_expression_allows_via_role(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1", roles=["ADMIN"])
        result = await async_admin_or_order_write()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_compound_expression_allows_via_permission(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=["order:write"],
        )
        result = await async_admin_or_order_write()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_compound_expression_raises_forbidden(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            roles=["USER"],
            permissions=["order:read"],
        )
        with pytest.raises(ForbiddenException, match="Access denied by expression"):
            await async_admin_or_order_write()


# ---------------------------------------------------------------------------
# @post_authorize - async tests
# ---------------------------------------------------------------------------


class TestPostAuthorizeAsync:
    @pytest.mark.asyncio
    async def test_allows_after_execution(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=["order:read"],
        )
        result = await async_post_order_read()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_raises_forbidden_after_execution(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=["order:write"],
        )
        with pytest.raises(ForbiddenException, match="Access denied by expression"):
            await async_post_order_read()

    @pytest.mark.asyncio
    async def test_method_body_runs_before_auth_check(self) -> None:
        side_effect_tracker.clear()
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=[],  # will fail the check
        )
        with pytest.raises(ForbiddenException):
            await async_post_with_side_effect()
        assert side_effect_tracker == ["executed"]

    @pytest.mark.asyncio
    async def test_raises_unauthorized_when_no_context(self) -> None:
        with pytest.raises(UnauthorizedException, match="Authentication required"):
            await async_post_order_read()


# ---------------------------------------------------------------------------
# @pre_authorize - sync tests
# ---------------------------------------------------------------------------


class TestPreAuthorizeSync:
    def test_is_authenticated_allows_authenticated_user(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1")
        result = sync_authenticated_only()
        assert result == "sync_ok"

    def test_is_authenticated_raises_when_no_security_context(self) -> None:
        RequestContext.init()
        with pytest.raises(UnauthorizedException, match="Authentication required"):
            sync_authenticated_only()

    def test_has_role_allows_matching_role(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1", roles=["ADMIN"])
        result = sync_admin_only()
        assert result == "sync_ok"

    def test_has_role_raises_forbidden_for_wrong_role(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(user_id="user1", roles=["USER"])
        with pytest.raises(ForbiddenException, match="Access denied by expression"):
            sync_admin_only()


# ---------------------------------------------------------------------------
# @post_authorize - sync tests
# ---------------------------------------------------------------------------


class TestPostAuthorizeSync:
    def test_allows_after_execution(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=["order:read"],
        )
        result = sync_post_order_read()
        assert result == "sync_ok"

    def test_raises_forbidden_after_execution(self) -> None:
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=[],
        )
        with pytest.raises(ForbiddenException, match="Access denied by expression"):
            sync_post_order_read()

    def test_method_body_runs_before_auth_check(self) -> None:
        sync_side_effect_tracker.clear()
        ctx = RequestContext.init()
        ctx.security_context = SecurityContext(
            user_id="user1",
            permissions=[],
        )
        with pytest.raises(ForbiddenException):
            sync_post_with_side_effect()
        assert sync_side_effect_tracker == ["executed"]


# ---------------------------------------------------------------------------
# Metadata attribute tests
# ---------------------------------------------------------------------------


class TestMetadataAttributes:
    def test_pre_authorize_sets_metadata(self) -> None:
        assert hasattr(async_admin_only, "__pyfly_pre_authorize__")
        assert async_admin_only.__pyfly_pre_authorize__ == "hasRole('ADMIN')"  # type: ignore[attr-defined]

    def test_post_authorize_sets_metadata(self) -> None:
        assert hasattr(async_post_order_read, "__pyfly_post_authorize__")
        assert async_post_order_read.__pyfly_post_authorize__ == "hasPermission('order:read')"  # type: ignore[attr-defined]

    def test_pre_authorize_preserves_function_name(self) -> None:
        assert async_admin_only.__name__ == "async_admin_only"

    def test_post_authorize_preserves_function_name(self) -> None:
        assert async_post_order_read.__name__ == "async_post_order_read"

    def test_sync_pre_authorize_sets_metadata(self) -> None:
        assert hasattr(sync_admin_only, "__pyfly_pre_authorize__")
        assert sync_admin_only.__pyfly_pre_authorize__ == "hasRole('ADMIN')"  # type: ignore[attr-defined]

    def test_sync_post_authorize_sets_metadata(self) -> None:
        assert hasattr(sync_post_order_read, "__pyfly_post_authorize__")
        assert sync_post_order_read.__pyfly_post_authorize__ == "hasPermission('order:read')"  # type: ignore[attr-defined]
