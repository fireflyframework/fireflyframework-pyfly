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
"""Method-security SpEL (v26.06.37): the expanded Spring Security expression vocabulary."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyfly.context.request_context import RequestContext
from pyfly.kernel.exceptions import ForbiddenException, SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.expression import evaluate_security_expression as ev
from pyfly.security.method_security import post_authorize, pre_authorize


@pytest.fixture(autouse=True)
def _clean_request_context() -> Iterator[None]:
    RequestContext.clear()
    yield
    RequestContext.clear()


def _ctx() -> SecurityContext:
    return SecurityContext(user_id="u1", roles=["ADMIN"], permissions=["order:read"])


# --- expression vocabulary --------------------------------------------------
def test_security_vocabulary() -> None:
    ctx = _ctx()
    assert ev("hasRole('ADMIN')", ctx) is True
    assert ev("hasAnyRole('X', 'ADMIN')", ctx) is True
    assert ev("hasAuthority('order:read')", ctx) is True  # permission counts as authority
    assert ev("hasAuthority('ADMIN')", ctx) is True  # role counts as authority
    assert ev("hasPermission('order:read')", ctx) is True
    assert ev("hasPermission('doc', 'order:read')", ctx) is True  # 2-arg (target, perm)
    assert ev("isAuthenticated()", ctx) is True
    assert ev("isAuthenticated", ctx) is True  # bare form still works
    assert ev("isAnonymous()", ctx) is False
    assert ev("permitAll", ctx) is True
    assert ev("denyAll", ctx) is False


def test_principal_and_param_and_return_object() -> None:
    ctx = _ctx()
    assert ev("principal.user_id == 'u1'", ctx) is True
    assert ev("authentication.is_authenticated", ctx) is True
    assert ev("#owner == principal.user_id", ctx, args={"owner": "u1"}) is True
    assert ev("#owner == principal.user_id", ctx, args={"owner": "u2"}) is False
    assert ev("returnObject == 7", ctx, return_object=7) is True


def test_boolean_combinations() -> None:
    ctx = _ctx()
    assert ev("hasRole('ADMIN') and isAuthenticated()", ctx) is True
    assert ev("hasRole('NOPE') or hasPermission('order:read')", ctx) is True
    assert ev("not hasRole('NOPE')", ctx) is True


def test_rejects_unsafe_constructs() -> None:
    ctx = _ctx()
    with pytest.raises(SecurityException):
        ev("principal.__class__", ctx)  # dunder attribute
    with pytest.raises(SecurityException):
        ev("principal.has_role('X')", ctx)  # arbitrary method call (not a security function)
    with pytest.raises(SecurityException):
        ev("unknown_name", ctx)  # unknown identifier


# --- decorators wire #param / returnObject ----------------------------------
@pytest.mark.asyncio
async def test_pre_authorize_with_param_reference() -> None:
    RequestContext.init().security_context = SecurityContext(user_id="u1", roles=["USER"])

    @pre_authorize("#owner == principal.user_id")
    async def view(owner: str) -> str:
        return f"viewing {owner}"

    assert await view(owner="u1") == "viewing u1"
    with pytest.raises(ForbiddenException):
        await view(owner="u2")


@pytest.mark.asyncio
async def test_post_authorize_with_return_object() -> None:
    RequestContext.init().security_context = SecurityContext(user_id="u1", roles=["USER"])

    def _doc(owner: str) -> object:
        return type("Doc", (), {"owner": owner})()

    @post_authorize("returnObject.owner == principal.user_id")
    async def get_mine() -> object:
        return _doc("u1")

    @post_authorize("returnObject.owner == principal.user_id")
    async def get_other() -> object:
        return _doc("u2")

    assert (await get_mine()).owner == "u1"
    with pytest.raises(ForbiddenException):
        await get_other()
