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
"""Real **Keycloak** integration test for the OAuth2 resource server.

Boots an actual Keycloak in Docker (testcontainers), provisions a realm, a
public client (direct-access grants), a realm role and a user via the Admin REST
API, then obtains a **real** access token from Keycloak's token endpoint and
validates it through PyFly's resource server:

* OIDC discovery against Keycloak's real ``/.well-known/openid-configuration``.
* Signature verification against Keycloak's real JWKS (``/protocol/openid-connect/certs``).
* ``iss`` / ``aud`` / ``exp`` validation and Keycloak realm-role claim mapping
  (``realm_access.roles``) onto a :class:`SecurityContext`.
* End-to-end through ``create_app`` + the auto-wired resource-server filter.

Marked ``integration`` (auto-applied by tests/integration/conftest.py); skips
when Docker is unavailable, fails hard in the CI integration job
(``PYFLY_INTEGRATION_REQUIRE_DOCKER=1``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from pyfly.testing import is_docker_available

pytestmark = pytest.mark.integration

KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:25.0.6"
REALM = "pyfly-test"
CLIENT_ID = "pyfly-api"
USERNAME = "alice"
PASSWORD = "alice-secret"
REALM_ROLE = "CdM.Gd"


# ---------------------------------------------------------------------------
# Keycloak container + provisioning
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def keycloak() -> Iterator[dict[str, str]]:
    """Start Keycloak, provision realm/client/role/user, yield connection info."""
    if not is_docker_available():
        pytest.skip("Docker not available for the Keycloak integration test")

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = (
        DockerContainer(KEYCLOAK_IMAGE)
        # Cover both the 25.x (KEYCLOAK_ADMIN) and 26.x (KC_BOOTSTRAP_ADMIN_*) names.
        .with_env("KEYCLOAK_ADMIN", "admin")
        .with_env("KEYCLOAK_ADMIN_PASSWORD", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin")
        .with_exposed_ports(8080)
        .with_command("start-dev")
    )
    with container:
        wait_for_logs(container, "Running the server in development mode", timeout=240)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8080)
        base = f"http://{host}:{port}"
        _provision(base)
        yield {"base": base, "issuer": f"{base}/realms/{REALM}"}


def _provision(base: str) -> None:
    """Create the realm, client, role and user via the Keycloak Admin REST API."""
    with httpx.Client(base_url=base, timeout=30.0) as http:
        admin = _retry_admin_token(http)
        h = {"Authorization": f"Bearer {admin}"}

        # Realm
        http.post("/admin/realms", headers=h, json={"realm": REALM, "enabled": True}).raise_for_status()

        # Public client with direct-access grants + an audience mapper so the
        # access token carries aud=pyfly-api (Keycloak's default aud is "account").
        http.post(
            f"/admin/realms/{REALM}/clients",
            headers=h,
            json={
                "clientId": CLIENT_ID,
                "publicClient": True,
                "directAccessGrantsEnabled": True,
                "standardFlowEnabled": False,
                "protocolMappers": [
                    {
                        "name": "aud-pyfly-api",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": CLIENT_ID,
                            "id.token.claim": "false",
                            "access.token.claim": "true",
                        },
                    }
                ],
            },
        ).raise_for_status()

        # Realm role
        http.post(f"/admin/realms/{REALM}/roles", headers=h, json={"name": REALM_ROLE}).raise_for_status()
        role = http.get(f"/admin/realms/{REALM}/roles/{REALM_ROLE}", headers=h).json()

        # User + password
        http.post(
            f"/admin/realms/{REALM}/users",
            headers=h,
            json={
                "username": USERNAME,
                "enabled": True,
                "credentials": [{"type": "password", "value": PASSWORD, "temporary": False}],
            },
        ).raise_for_status()
        uid = http.get(f"/admin/realms/{REALM}/users", headers=h, params={"username": USERNAME}).json()[0]["id"]

        # Assign the realm role to the user
        http.post(
            f"/admin/realms/{REALM}/users/{uid}/role-mappings/realm",
            headers=h,
            json=[{"id": role["id"], "name": role["name"]}],
        ).raise_for_status()


def _retry_admin_token(http: httpx.Client, attempts: int = 30) -> str:
    """Fetch a master-realm admin token, retrying until Keycloak is ready."""
    import time

    last: Exception | None = None
    for _ in range(attempts):
        try:
            resp = http.post(
                "/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": "admin",
                    "password": "admin",
                },
            )
            resp.raise_for_status()
            return str(resp.json()["access_token"])
        except Exception as exc:  # not ready yet
            last = exc
            time.sleep(2)
    raise RuntimeError(f"Keycloak admin token never became available: {last}")


def _user_access_token(base: str) -> str:
    """Obtain a real user access token via the resource-owner password grant."""
    with httpx.Client(base_url=base, timeout=30.0) as http:
        resp = http.post(
            f"/realms/{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": USERNAME,
                "password": PASSWORD,
                "scope": "openid profile",
            },
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_oidc_discovery_against_real_keycloak(keycloak: dict[str, str]) -> None:
    from pyfly.security.oauth2.resource_server import discover_oidc

    jwks_uri, issuer = discover_oidc(keycloak["issuer"])
    assert jwks_uri.startswith(keycloak["base"])
    assert "/protocol/openid-connect/certs" in jwks_uri
    assert issuer == keycloak["issuer"]


def test_real_keycloak_token_validates_and_maps_roles(keycloak: dict[str, str]) -> None:
    from pyfly.security.oauth2.resource_server import JWKSTokenValidator, discover_oidc

    token = _user_access_token(keycloak["base"])
    jwks_uri, issuer = discover_oidc(keycloak["issuer"])
    validator = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=issuer, audiences=[CLIENT_ID])

    payload = validator.validate(token)
    assert payload["iss"] == issuer
    assert REALM_ROLE in payload["realm_access"]["roles"]

    ctx = validator.to_security_context(token)
    assert ctx.is_authenticated
    assert REALM_ROLE in ctx.roles  # realm_access.roles -> SecurityContext.roles


def test_real_keycloak_rejects_tampered_token(keycloak: dict[str, str]) -> None:
    from pyfly.kernel.exceptions import SecurityException
    from pyfly.security.oauth2.resource_server import JWKSTokenValidator, discover_oidc

    token = _user_access_token(keycloak["base"])
    jwks_uri, issuer = discover_oidc(keycloak["issuer"])
    validator = JWKSTokenValidator(jwks_uri=jwks_uri, issuer=issuer, audiences=[CLIENT_ID])

    # Flip a character in the signature segment.
    head, body, sig = token.split(".")
    tampered = f"{head}.{body}.{sig[:-3]}{'AAA' if sig[-3:] != 'AAA' else 'BBB'}"
    with pytest.raises(SecurityException):
        validator.validate(tampered)


@pytest.mark.asyncio
async def test_real_keycloak_end_to_end_through_filter(keycloak: dict[str, str]) -> None:
    from starlette.testclient import TestClient

    from pyfly.container.stereotypes import rest_controller
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.web.adapters.starlette.app import create_app
    from pyfly.web.mappings import get_mapping, request_mapping

    @rest_controller
    @request_mapping("/api/me")
    class MeController:
        @get_mapping("/")
        async def me(self) -> dict:
            from pyfly.context.request_context import RequestContext

            rc = RequestContext.current()
            sc = rc.security_context if rc is not None else None
            return {"user": sc.user_id if sc else None, "roles": sc.roles if sc else []}

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "security": {
                        "enabled": "true",
                        "oauth2": {
                            "resource-server": {
                                "enabled": "true",
                                "issuer-uri": keycloak["issuer"],  # OIDC discovery
                                "audiences": CLIENT_ID,
                            }
                        },
                    }
                }
            }
        )
    )
    ctx.register_bean(MeController)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[None]:
        await ctx.start()
        yield
        await ctx.stop()

    app = create_app(context=ctx, lifespan=_lifespan)
    token = _user_access_token(keycloak["base"])
    with TestClient(app) as client:
        resp = client.get("/api/me/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"] is not None
        assert REALM_ROLE in body["roles"]
