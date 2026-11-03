# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Read-only source consent over the native bounded OAuth acquisition client."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Self
from urllib.parse import urlencode
from uuid import UUID

import httpx

from pyfly.agentic.content.sources.dtos import Provider, bounded
from pyfly.agentic.content.sources.errors import SourceError
from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints, OAuth2Tokens
from pyfly.oauth2.acquisition import _endpoint, _scope_value

_GOOGLE_READ = "https://www.googleapis.com/auth/drive.readonly"
_GRAPH_READ = "https://graph.microsoft.com/Files.Read.All"


@dataclass(frozen=True, slots=True)
class SourceOAuthConfig:
    provider: Provider
    client_id: str
    redirect_uri: str
    tenant_id: str | None = None
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        bounded(self.client_id)
        bounded(self.redirect_uri, limit=2048)
        if not isinstance(self.allow_loopback_http, bool):
            raise ValueError("Loopback HTTP requires an explicit boolean opt-in")
        _endpoint(self.redirect_uri, self.allow_loopback_http)
        if self.provider not in {"sharepoint", "onedrive", "google_drive"}:
            raise ValueError("Unsupported source provider")
        if self.provider != "google_drive":
            if not (self.provider == "onedrive" and self.tenant_id == "consumers"):
                try:
                    if str(UUID(self.tenant_id or "")) != self.tenant_id:
                        raise ValueError
                except ValueError:
                    raise ValueError(
                        "Microsoft source consent requires an explicit tenant UUID or OneDrive consumers"
                    ) from None
        elif self.tenant_id is not None:
            raise ValueError("Google workspace identity restrictions belong to verified callback policy")

    @property
    def scopes(self) -> tuple[str, ...]:
        return (
            ("openid", "profile", _GOOGLE_READ)
            if self.provider == "google_drive"
            else ("openid", "profile", "offline_access", _GRAPH_READ)
        )

    @property
    def authorization_endpoint(self) -> str:
        return (
            "https://accounts.google.com/o/oauth2/v2/auth"
            if self.provider == "google_drive"
            else (f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/authorize")
        )

    @property
    def token_endpoint(self) -> str:
        return (
            "https://oauth2.googleapis.com/token"
            if self.provider == "google_drive"
            else (f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token")
        )


class ReadOnlySourceOAuth:
    """Native code/refresh acquisition, never a login or callback authenticator.

    The application MUST verify callback state, exact URI, expected issuer and
    replay before exchange. Verify ID-token signature, issuer, audience, expiry
    and nonce before binding provider identity and persisting an Account. ID tokens
    remain opaque here. Encrypt refresh credentials and
    serialize rotation in durable application storage. No client-credentials
    operation or automatic scope expansion is exposed.
    """

    def __init__(
        self,
        config: SourceOAuthConfig,
        *,
        client_secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = OAuth2Client(
            config.client_id, OAuth2Endpoints(config.token_endpoint), client_secret=client_secret, transport=transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    def authorization_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        bounded(state)
        bounded(nonce)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge):
            raise ValueError("An S256 PKCE challenge is required")
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        if self.config.provider == "google_drive":
            params.update(access_type="offline", prompt="consent")
        return self.config.authorization_endpoint + "?" + urlencode(params)

    def _granted(self, scopes: tuple[str, ...]) -> frozenset[str]:
        try:
            _scope_value(scopes)
        except ValueError:
            raise SourceError("scope_mismatch") from None
        aliases = (
            {"https://www.googleapis.com/auth/userinfo.profile": "profile"}
            if self.config.provider == "google_drive"
            else {"Files.Read.All": _GRAPH_READ}
        )
        granted = frozenset(aliases.get(scope, scope) for scope in scopes)
        required = _GOOGLE_READ if self.config.provider == "google_drive" else _GRAPH_READ
        if required not in granted or not granted <= set(self.config.scopes):
            raise SourceError("scope_mismatch")
        return granted

    def _checked(self, tokens: OAuth2Tokens, *, ceiling: frozenset[str] | None = None) -> OAuth2Tokens:
        granted = self._granted(tuple((tokens.scope or "").split(" ")))
        if tokens.token_type.lower() != "bearer" or (ceiling is not None and not granted <= ceiling):
            raise SourceError("scope_mismatch")
        return tokens

    async def exchange_code(self, code: str, *, code_verifier: str) -> OAuth2Tokens:
        return self._checked(
            await self._client.exchange_code(code, redirect_uri=self.config.redirect_uri, code_verifier=code_verifier)
        )

    async def refresh_token(self, refresh_token: str, *, granted_scopes: tuple[str, ...]) -> OAuth2Tokens:
        """Refresh only a trusted persisted grant; never infer it from configuration.

        An omitted response scope means the same scope requested (RFC 6749
        section 5.1). Preserve that effective scope in the returned token DTO.
        The caller retains the prior refresh token when no rotation is returned.
        """
        previous = self._granted(granted_scopes)
        requested = tuple(sorted(previous))
        tokens = await self._client.refresh_token(refresh_token, scopes=requested)
        if tokens.scope is None:
            tokens = replace(tokens, scope=" ".join(requested))
        return self._checked(tokens, ceiling=previous)
