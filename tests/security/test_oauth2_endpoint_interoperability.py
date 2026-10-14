# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Public native acquisition client against the stock AS HTTP routes, including real TCP."""

from __future__ import annotations

import asyncio
import base64
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints, generate_pkce
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints

Authentication = Literal["client_secret_basic", "client_secret_post"]
CALLBACK = "https://app.example/callback"


class FixtureOwnerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.security_context = SecurityContext(user_id="fixture-resource-owner")
        return await call_next(request)  # type: ignore[no-any-return]


@asynccontextmanager
async def provider(
    mode: str, registration: ClientRegistration
) -> AsyncIterator[tuple[AuthorizationServer, httpx.AsyncClient, OAuth2Client]]:
    listener: socket.socket | None = None
    server: uvicorn.Server | None = None
    task: asyncio.Task[None] | None = None
    origin = "https://as.example"
    if mode == "tcp":
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    native = AuthorizationServer(
        "owned-local-test-signing-key-32-bytes",
        InMemoryClientRegistrationRepository(registration),
        InMemoryTokenStore(),
        issuer=origin,
        audience="https://service.example",
    )
    app = Starlette(routes=AuthorizationServerEndpoints(native).routes())
    app.add_middleware(FixtureOwnerMiddleware)
    if listener is not None:
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
    transport = httpx.ASGITransport(app=app) if mode == "asgi" else None
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url=origin, trust_env=False, follow_redirects=False) as browser,
            OAuth2Client(
                registration.client_id,
                OAuth2Endpoints(origin + "/oauth2/token"),
                client_secret=registration.client_secret or None,
                transport=httpx.ASGITransport(app=app) if mode == "asgi" else None,
                allow_loopback_http=mode == "tcp",
            ) as acquisition,
        ):
            yield native, browser, acquisition
    finally:
        if server and task:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
        if listener:
            listener.close()


def registration(
    *, public: bool = False, machine: bool = False, client_id: str = "client", secret: str = "client-secret"
) -> ClientRegistration:
    return ClientRegistration(
        registration_id=client_id,
        client_id=client_id,
        client_secret="" if public else secret,
        authorization_grant_type="client_credentials" if machine else "authorization_code",
        redirect_uri=CALLBACK,
        scopes=["read"],
    )


async def code(browser: httpx.AsyncClient, client_id: str) -> tuple[str, str]:
    pair = generate_pkce()
    response = await browser.get(
        "/oauth2/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "scope": "read",
            "state": "owned-fixture-state",
            "code_challenge": pair.challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["owned-fixture-state"]
    return query["code"][0], pair.verifier


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize(
    "authentication,public",
    [("client_secret_basic", False), ("client_secret_post", False), ("client_secret_post", True)],
)
async def test_public_client_exchanges_pkce_code_through_stock_http_routes(
    mode: str, authentication: Authentication, public: bool
) -> None:
    async with provider(mode, registration(public=public)) as (_, browser, client):
        authorization_code, verifier = await code(browser, "client")
        result = await client.exchange_code(
            authorization_code, redirect_uri=CALLBACK, code_verifier=verifier, authentication=authentication
        )
        assert result.token_type.lower() == "bearer" and result.access_token and result.refresh_token


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize("wrong", ["verifier", "redirect", "code"])
async def test_invalid_pkce_binding_is_rejected_without_consuming_the_valid_code(mode: str, wrong: str) -> None:
    async with provider(mode, registration()) as (_, browser, client):
        authorization_code, verifier = await code(browser, "client")
        with pytest.raises(OAuth2ClientError) as rejected:
            await client.exchange_code(
                "unknown-code" if wrong == "code" else authorization_code,
                redirect_uri="https://other.example/callback" if wrong == "redirect" else CALLBACK,
                code_verifier=generate_pkce().verifier if wrong == "verifier" else verifier,
            )
        assert rejected.value.code == "invalid_grant"
        result = await client.exchange_code(authorization_code, redirect_uri=CALLBACK, code_verifier=verifier)
        assert result.access_token


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
async def test_code_replay_rejects_and_revokes_the_original_native_refresh_family(mode: str) -> None:
    async with provider(mode, registration()) as (native, browser, client):
        authorization_code, verifier = await code(browser, "client")
        issued = await client.exchange_code(authorization_code, redirect_uri=CALLBACK, code_verifier=verifier)
        with pytest.raises(OAuth2ClientError) as replay:
            await client.exchange_code(authorization_code, redirect_uri=CALLBACK, code_verifier=verifier)
        assert replay.value.code == "invalid_grant"
        assert issued.refresh_token is not None
        assert (await native.introspect(issued.refresh_token))["active"] is False


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize(
    "client_id,secret",
    [
        ("client+name", "secret+value"),
        ("client%name", "secret%value"),
        ("client:name", "secret:with:colons"),
        ("client café", "sëcret value"),
    ],
)
async def test_basic_credentials_interoperate_with_public_form_encoding(mode: str, client_id: str, secret: str) -> None:
    async with provider(mode, registration(machine=True, client_id=client_id, secret=secret)) as (native, _, client):
        issued = await native.token(grant_type="client_credentials", client_id=client_id, client_secret=secret)
        rotated = await client.refresh_token(issued["refresh_token"], authentication="client_secret_basic")
        assert rotated.refresh_token and rotated.refresh_token != issued["refresh_token"]


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize("authentication", ["client_secret_basic", "client_secret_post"])
async def test_existing_machine_and_refresh_http_contracts_remain_compatible(
    mode: str, authentication: Authentication
) -> None:
    async with provider(mode, registration(machine=True)) as (_, browser, client):
        form = {"grant_type": "client_credentials", "scope": "read"}
        if authentication == "client_secret_post":
            form.update(client_id="client", client_secret="client-secret")
        response = await browser.post(
            "/oauth2/token",
            data=form,
            auth=httpx.BasicAuth("client", "client-secret") if authentication == "client_secret_basic" else None,
        )
        assert response.status_code == 200
        issued = response.json()
        rotated = await client.refresh_token(issued["refresh_token"], authentication=authentication)
        assert rotated.refresh_token and rotated.refresh_token != issued["refresh_token"]


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize("field", ["code", "redirect_uri", "code_verifier"])
async def test_http_code_request_requires_all_binding_fields_without_consuming_code(mode: str, field: str) -> None:
    async with provider(mode, registration()) as (_, browser, client):
        authorization_code, verifier = await code(browser, "client")
        body = {
            "grant_type": "authorization_code",
            "client_id": "client",
            "client_secret": "client-secret",
            "code": authorization_code,
            "redirect_uri": CALLBACK,
            "code_verifier": verifier,
        }
        body.pop(field)
        response = await browser.post("/oauth2/token", data=body)
        assert response.status_code == 400
        assert response.json()["error"] in {"invalid_request", "invalid_grant"}
        issued = await client.exchange_code(authorization_code, redirect_uri=CALLBACK, code_verifier=verifier)
        assert issued.access_token


@pytest.mark.parametrize("mode", ["asgi", "tcp"])
@pytest.mark.parametrize("verifier", ["café" * 16, "v" * 129, "v" * 42, "v" * 63 + "\n"])
async def test_http_malformed_verifier_is_a_protocol_error_and_never_consumes_code(mode: str, verifier: str) -> None:
    async with provider(mode, registration()) as (_, browser, client):
        authorization_code, correct_verifier = await code(browser, "client")
        response = await browser.post(
            "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "client",
                "client_secret": "client-secret",
                "code": authorization_code,
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 400
        assert verifier not in response.text
        issued = await client.exchange_code(authorization_code, redirect_uri=CALLBACK, code_verifier=correct_verifier)
        assert issued.access_token


@pytest.mark.parametrize(
    "client_id,secret",
    [
        ("client", "client-secret"),
        ("client+name", "secret+value"),
        ("client%name", "secret%value"),
        ("client:name", "secret:with:colons"),
        ("client café", "sëcret value"),
    ],
)
async def test_native_opaque_introspector_uses_oauth_encoding_and_preserves_ascii_wire(
    client_id: str, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyfly.security.oauth2.resource_server import OpaqueTokenIntrospector

    captured = []
    client_type = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["event_hooks"] = {"request": [lambda request: captured.append(request.headers["authorization"])]}
        return client_type(*args, **kwargs)

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1")
    monkeypatch.setattr(httpx, "Client", client_factory)
    async with provider("tcp", registration(machine=True, client_id=client_id, secret=secret)) as (native, browser, _):
        issued = await native.token(grant_type="client_credentials", client_id=client_id, client_secret=secret)
        introspector = OpaqueTokenIntrospector(
            str(browser.base_url) + "/oauth2/introspect", client_id=client_id, client_secret=secret
        )
        claims = await asyncio.to_thread(introspector.introspect, issued["access_token"])
        assert claims["active"] is True and claims["sub"] == client_id
        expected = "Basic " + base64.b64encode((quote_plus(client_id) + ":" + quote_plus(secret)).encode()).decode()
        assert captured == [expected]
        if client_id == "client":
            assert base64.b64decode(captured[0][6:]) == b"client:client-secret"
