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
"""Security WebFilter wiring is live after startup (audit #41, #42, #45, #46)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security.context import SecurityContext
from pyfly.security.jwt import JWTService
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter
from pyfly.web.mappings import get_mapping, request_mapping

_SECRET = "wave-security-test-secret-key-32b!!"


@rest_controller
@request_mapping("/api/me")
class WhoAmIController:
    @get_mapping("/")
    async def me(self) -> dict:
        from pyfly.context.request_context import RequestContext

        ctx = RequestContext.current()
        sc = ctx.security_context if ctx is not None else None
        return {"user": sc.user_id if sc is not None else None}


def _build_app() -> tuple[Any, ApplicationContext]:
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "security": {"enabled": "true", "jwt": {"secret": _SECRET, "filter": {"enabled": "true"}}},
                }
            }
        )
    )
    ctx.register_bean(WhoAmIController)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()
        yield
        await ctx.stop()

    return create_app(context=ctx, lifespan=_lifespan), ctx


@pytest.mark.asyncio
async def test_jwt_security_filter_populates_context_after_start() -> None:
    app, _ctx = _build_app()
    token = JWTService(secret=_SECRET).encode({"sub": "alice", "roles": ["USER"]})
    with TestClient(app) as client:
        resp = client.get("/api/me/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json() == {"user": "alice"}


@pytest.mark.asyncio
async def test_jwt_security_filter_anonymous_without_token() -> None:
    app, _ctx = _build_app()
    with TestClient(app) as client:
        resp = client.get("/api/me/")
        assert resp.status_code == 200
        assert resp.json() == {"user": None}


@pytest.mark.asyncio
async def test_security_filter_bean_is_in_live_chain() -> None:
    app, _ctx = _build_app()
    with TestClient(app):  # trigger startup so the post-start rescan runs
        # The middleware instance holds the live filter list; assert the
        # SecurityFilter joined it after start().
        for mw in app.user_middleware:
            holder = getattr(mw, "kwargs", {})
            filters = holder.get("filters", [])
            if any(isinstance(f, SecurityFilter) for f in filters):
                break
        else:  # pragma: no cover - the shared list is the same object
            pytest.fail("SecurityFilter was not added to the live filter chain")


def test_redis_session_store_round_trips_security_context() -> None:
    # Audit #46: a SecurityContext attribute must survive the JSON round-trip.
    import json

    from pyfly.session.adapters.redis import _json_default, _json_object_hook

    sc = SecurityContext(user_id="bob", roles=["ADMIN"], permissions=["read"])
    raw = json.dumps({"SECURITY_CONTEXT": sc}, default=_json_default)
    restored = json.loads(raw, object_hook=_json_object_hook)["SECURITY_CONTEXT"]
    assert isinstance(restored, SecurityContext)
    assert restored.user_id == "bob"
    assert restored.roles == ["ADMIN"]
    assert restored.permissions == ["read"]
