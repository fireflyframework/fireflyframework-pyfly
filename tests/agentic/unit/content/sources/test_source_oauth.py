# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from pyfly.agentic.content.sources.errors import SourceError
from pyfly.agentic.content.sources.oauth import ReadOnlySourceOAuth, SourceOAuthConfig
from pyfly.oauth2 import generate_pkce


@pytest.mark.parametrize("provider", ["sharepoint", "onedrive", "google_drive"])
async def test_native_delegated_code_and_rotated_refresh_contract(provider):
    config = SourceOAuthConfig(
        provider,
        "public-client",
        "https://app.example/source-callback",
        tenant_id=None if provider == "google_drive" else "12345678-1234-1234-1234-123456789012",
    )
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "POST"
        data = parse_qs(request.content.decode())
        assert data["grant_type"] in [["authorization_code"], ["refresh_token"]]
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "token_type": "Bearer",
                "refresh_token": "synthetic-rotated-refresh",
                "expires_in": 3600,
                "scope": " ".join(config.scopes),
            },
        )

    async with ReadOnlySourceOAuth(config, transport=httpx.MockTransport(handler)) as oauth:
        pkce = generate_pkce()
        url = oauth.authorization_url(state="random-bound-state", code_challenge=pkce.challenge, nonce="bound-nonce")
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"] and query["response_type"] == ["code"]
        assert "write" not in query["scope"][0].lower()
        grant = await oauth.exchange_code("synthetic-code", code_verifier=pkce.verifier)
        rotated = await oauth.refresh_token(grant.refresh_token, granted_scopes=tuple(grant.scope.split()))
    assert len(seen) == 2 and rotated.refresh_token == "synthetic-rotated-refresh"
    assert "synthetic-access" not in repr(rotated) and "synthetic-rotated-refresh" not in repr(rotated)
    assert "client_credentials" not in json.dumps([parse_qs(r.content.decode()) for r in seen])


@pytest.mark.parametrize("scope", ["Files.ReadWrite.All", "https://www.googleapis.com/auth/drive.file", None])
async def test_wider_or_missing_granted_scopes_are_not_accepted(scope):
    config = SourceOAuthConfig("onedrive", "client", "https://app.example/callback", tenant_id="consumers")
    async with ReadOnlySourceOAuth(
        config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "access_token": "private",
                    "token_type": "Bearer",
                    **({"scope": scope} if scope is not None else {}),
                },
            )
        ),
    ) as oauth:
        with pytest.raises(SourceError, match="scope_mismatch"):
            await oauth.exchange_code("code", code_verifier=generate_pkce().verifier)


async def test_google_exact_profile_alias_is_accepted_without_discarding_extra_scopes():
    config = SourceOAuthConfig("google_drive", "client", "https://app.example/callback")
    base = "openid https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/drive.readonly"
    for extra in ("", " https://www.googleapis.com/auth/userinfo.email", " https://www.googleapis.com/auth/drive"):
        async with ReadOnlySourceOAuth(
            config,
            transport=httpx.MockTransport(
                lambda request, extra=extra: httpx.Response(
                    200, json={"access_token": "synthetic", "token_type": "Bearer", "scope": base + extra}
                )
            ),
        ) as oauth:
            if extra:
                with pytest.raises(SourceError, match="scope_mismatch"):
                    await oauth.exchange_code("code", code_verifier=generate_pkce().verifier)
            else:
                tokens = await oauth.exchange_code("code", code_verifier=generate_pkce().verifier)
                assert tokens.scope == base


async def test_refresh_omitted_scope_uses_exact_previously_granted_subset():
    config = SourceOAuthConfig("google_drive", "client", "https://app.example/callback")
    granted = ("https://www.googleapis.com/auth/drive.readonly",)
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "synthetic-new", "token_type": "Bearer"})

    async with ReadOnlySourceOAuth(config, transport=httpx.MockTransport(handler)) as oauth:
        tokens = await oauth.refresh_token("synthetic-refresh", granted_scopes=granted)
    assert tokens.scope == granted[0]
    assert seen[0]["scope"] == [granted[0]]
    assert tokens.refresh_token is None  # Caller must retain the stored grant when it is not rotated.


@pytest.mark.parametrize("scope", ["openid https://www.googleapis.com/auth/drive.readonly", "openid"])
async def test_refresh_explicit_scope_cannot_expand_or_lose_required_read(scope):
    config = SourceOAuthConfig("google_drive", "client", "https://app.example/callback")
    async with ReadOnlySourceOAuth(
        config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"access_token": "synthetic", "token_type": "Bearer", "scope": scope}
            )
        ),
    ) as oauth:
        with pytest.raises(SourceError, match="scope_mismatch"):
            await oauth.refresh_token("refresh", granted_scopes=("https://www.googleapis.com/auth/drive.readonly",))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_http_callback_requires_explicit_exact_loopback_opt_in(host):
    callback = f"http://{host}:8321/source/callback"
    with pytest.raises(ValueError):
        SourceOAuthConfig("google_drive", "client", callback)
    config = SourceOAuthConfig("google_drive", "client", callback, allow_loopback_http=True)
    assert config.redirect_uri == callback
    assert config.token_endpoint.startswith("https://")


@pytest.mark.parametrize(
    "uri",
    [
        "http://0.0.0.0/callback",
        "http://localhost.evil.example/callback",
        "http://127.0.0.2/callback",
        "http://2130706433/callback",
        "http://localhost:invalid/callback",
        "http://user@localhost/callback",
        "http://localhost/callback#fragment",
    ],
)
def test_loopback_exception_does_not_allow_other_http_callbacks(uri):
    with pytest.raises(ValueError):
        SourceOAuthConfig("google_drive", "client", uri, allow_loopback_http=True)


@pytest.mark.parametrize(
    "provider,scopes",
    [
        ("google_drive", ("openid", "profile")),
        ("google_drive", ("https://www.googleapis.com/auth/drive.readonly", "unknown")),
        (
            "google_drive",
            ("https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/userinfo.email"),
        ),
        ("onedrive", ("https://graph.windows.net/Files.Read.All",)),
        ("onedrive", ("Files.Read.All", "Files.ReadWrite.All")),
    ],
)
async def test_invalid_prior_grant_is_rejected_before_any_token_request(provider, scopes):
    config = SourceOAuthConfig(
        provider, "client", "https://app.example/callback", tenant_id="consumers" if provider == "onedrive" else None
    )
    seen = []
    async with ReadOnlySourceOAuth(
        config, transport=httpx.MockTransport(lambda request: seen.append(request))
    ) as oauth:
        with pytest.raises(SourceError, match="scope_mismatch"):
            await oauth.refresh_token("refresh", granted_scopes=scopes)
    assert not seen


@pytest.mark.parametrize(
    "uri", ["http://192.168.1.5/callback", "http://10.0.0.5/callback", "http://public.example/callback"]
)
def test_loopback_opt_in_keeps_lan_and_external_callbacks_https_only(uri):
    with pytest.raises(ValueError):
        SourceOAuthConfig("google_drive", "client", uri, allow_loopback_http=True)
