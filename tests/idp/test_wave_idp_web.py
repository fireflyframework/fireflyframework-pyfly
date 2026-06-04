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
"""The IDP REST controller is wired and reachable (audit #22, #25)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.idp.port import IdpAdapter
from pyfly.web.adapters.starlette.app import create_app


def _build_app() -> tuple[Any, ApplicationContext]:
    ctx = ApplicationContext(Config({"pyfly": {"idp": {"enabled": "true"}}}))

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()
        yield
        await ctx.stop()

    return create_app(context=ctx, lifespan=_lifespan), ctx


@pytest.mark.asyncio
async def test_idp_login_endpoint_reachable() -> None:
    app, _ctx = _build_app()
    with TestClient(app) as client:
        # Create a user, then log in through the HTTP surface.
        created = client.post(
            "/idp/admin/users",
            json={"username": "alice", "password": "s3cret!!", "email": "a@x.io"},
        )
        assert created.status_code == 200
        resp = client.post("/idp/login", json={"username": "alice", "password": "s3cret!!"})
        assert resp.status_code == 200
        assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_idp_adapter_registered_under_port_type() -> None:
    _app, ctx = _build_app()
    await ctx.start()
    try:
        adapter = ctx.get_bean(IdpAdapter)  # resolvable by the port type
        assert adapter is not None
    finally:
        await ctx.stop()


def test_provider_selection_defaults_internal_db() -> None:
    from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter
    from pyfly.idp.auto_configuration import IdpAutoConfiguration

    cfg = Config({"pyfly": {"idp": {"enabled": "true"}}})
    adapter = IdpAutoConfiguration().idp_adapter(cfg)
    assert isinstance(adapter, InternalDbIdpAdapter)


# --- #23 Cognito SECRET_HASH + #29 internal-db roles ---


class _FakeBoto:
    def __init__(self) -> None:
        self.auth_params: dict = {}

    def initiate_auth(self, **kwargs):  # noqa: ANN003
        self.auth_params = kwargs.get("AuthParameters", {})
        return {"AuthenticationResult": {"AccessToken": "tok", "RefreshToken": "r", "ExpiresIn": 3600}}

    def admin_get_user(self, **kwargs):  # noqa: ANN003
        raise Exception("no user")  # forces get_user → None fallback


@pytest.mark.asyncio
async def test_cognito_login_includes_secret_hash() -> None:
    from pyfly.idp.adapters.aws_cognito import AwsCognitoIdpAdapter
    from pyfly.idp.models import LoginRequest

    fake = _FakeBoto()
    adapter = AwsCognitoIdpAdapter(
        user_pool_id="pool", client_id="cid", region="us-east-1", client_secret="shh", client=fake
    )
    await adapter.login(LoginRequest(username="bob", password="pw"))
    assert "SECRET_HASH" in fake.auth_params  # audit #23


@pytest.mark.asyncio
async def test_internal_db_assign_role_populates_catalogue() -> None:
    from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter
    from pyfly.idp.models import IdpUser

    adapter = InternalDbIdpAdapter()
    user = await adapter.create_user(IdpUser(username="carol"), "pw123456")
    await adapter.assign_role(user.id, "ADMIN")
    roles = await adapter.list_roles()
    assert any(r.name == "ADMIN" for r in roles)  # audit #29
