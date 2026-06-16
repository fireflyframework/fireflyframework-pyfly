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
"""OAuth2 resource-server auto-configuration wiring.

End-to-end through ``create_app``: the auto-config binds
``ResourceServerProperties``, builds a multi-IdP ``JWKSTokenValidator``, and the
resource-server filter joins the live chain and populates the request principal.
Also pins the ``@conditional_on_missing_bean(JWKSTokenValidator)`` back-off that
lets an application register its own validator subclass (the cdm-mexico
``EntraClaimsValidator`` pattern).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.resource_server import JWKSTokenValidator
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.mappings import get_mapping, request_mapping

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_AUTHORITY_CLAIMS = "roles,realm_access.roles,resource_access.*.roles,groups,cognito:groups"


def _jwk(kid: str) -> dict[str, Any]:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    data.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return data


@pytest.fixture()
def jwks_uri() -> Iterator[str]:
    doc = {"keys": [_jwk("k1")]}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/jwks"
    finally:
        httpd.shutdown()


def _token(**claims: Any) -> str:
    body = {"exp": int(time.time()) + 3600, **claims}
    return jwt.encode(body, _KEY, algorithm="RS256", headers={"kid": "k1"})


@rest_controller
@request_mapping("/api/whoami")
class WhoAmIController:
    @get_mapping("/")
    async def whoami(self) -> dict:
        from pyfly.context.request_context import RequestContext

        ctx = RequestContext.current()
        sc = ctx.security_context if ctx is not None else None
        return {
            "user": sc.user_id if sc else None,
            "roles": sc.roles if sc else [],
            "perms": sc.permissions if sc else [],
        }


def _resource_server_config(jwks: str) -> dict[str, Any]:
    return {
        "pyfly": {
            "security": {
                "enabled": "true",
                "oauth2": {
                    "resource-server": {
                        "enabled": "true",
                        "jwks-uri": jwks,
                        "issuer": "https://kc.example.com/realms/cdm",
                        "audiences": "cdm-api",
                        "scope-claim-names": "scp,scope",
                        "authorities-claim-names": _AUTHORITY_CLAIMS,
                    }
                },
            }
        }
    }


def _lifespan_for(ctx: ApplicationContext) -> Any:
    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()
        yield
        await ctx.stop()

    return _lifespan


def _build_app(config: dict[str, Any]) -> tuple[Any, ApplicationContext]:
    ctx = ApplicationContext(Config(config))
    ctx.register_bean(WhoAmIController)
    return create_app(context=ctx, lifespan=_lifespan_for(ctx)), ctx


@pytest.mark.asyncio
async def test_keycloak_token_populates_principal_end_to_end(jwks_uri: str) -> None:
    app, _ = _build_app(_resource_server_config(jwks_uri))
    token = _token(
        iss="https://kc.example.com/realms/cdm",
        aud="cdm-api",
        sub="kc-user",
        realm_access={"roles": ["CdM.Gd"]},
        resource_access={"cdm-api": {"roles": ["client-x"]}},
        scope="read write",
    )
    with TestClient(app) as client:
        resp = client.get("/api/whoami/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"] == "kc-user"
        assert "CdM.Gd" in body["roles"] and "client-x" in body["roles"]
        assert body["perms"] == ["read", "write"]


@pytest.mark.asyncio
async def test_no_token_is_anonymous_end_to_end(jwks_uri: str) -> None:
    app, _ = _build_app(_resource_server_config(jwks_uri))
    with TestClient(app) as client:
        resp = client.get("/api/whoami/")
        assert resp.status_code == 200
        assert resp.json() == {"user": None, "roles": [], "perms": []}


@pytest.mark.asyncio
async def test_user_validator_subclass_overrides_default(jwks_uri: str) -> None:
    # cdm-mexico registers its own JWKSTokenValidator subclass; the default
    # auto-config bean must back off (@conditional_on_missing_bean is subclass-aware).
    class CustomValidator(JWKSTokenValidator):
        def to_security_context(self, token: str) -> SecurityContext:  # noqa: ARG002
            return SecurityContext(user_id="fixed-by-subclass", roles=["CdM.Gn"])

    ctx = ApplicationContext(Config(_resource_server_config(jwks_uri)))
    ctx.register_bean(WhoAmIController)
    # Register the subclass as the base JWKSTokenValidator type (cdm pattern).
    ctx.container.register_instance(JWKSTokenValidator, CustomValidator(jwks_uri=jwks_uri))
    app = create_app(context=ctx, lifespan=_lifespan_for(ctx))
    with TestClient(app) as client:
        # Even a garbage token yields the subclass's fixed identity → proves the
        # subclass validator (not the default) is wired into the filter.
        resp = client.get("/api/whoami/", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 200
        assert resp.json()["user"] == "fixed-by-subclass"

    validators = ctx.get_beans_of_type(JWKSTokenValidator)
    assert len(validators) == 1
    assert isinstance(validators[0], CustomValidator)
