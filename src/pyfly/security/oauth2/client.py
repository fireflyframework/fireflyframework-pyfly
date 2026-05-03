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
"""OAuth2 Client Registration — provider configuration and repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ClientRegistration:
    """OAuth2 client registration configuration.

    Represents the configuration needed to interact with an OAuth2 provider
    as a client application.
    """

    registration_id: str
    client_id: str
    client_secret: str = ""
    authorization_grant_type: str = "authorization_code"
    redirect_uri: str = ""
    scopes: list[str] = field(default_factory=list)
    authorization_uri: str = ""
    token_uri: str = ""
    user_info_uri: str = ""
    jwks_uri: str = ""
    issuer_uri: str = ""
    provider_name: str = ""


# ---------------------------------------------------------------------------
# Built-in provider factories
# ---------------------------------------------------------------------------


def google(
    client_id: str,
    client_secret: str,
    redirect_uri: str = "",
) -> ClientRegistration:
    """Create a ClientRegistration pre-configured for Google OAuth2."""
    return ClientRegistration(
        registration_id="google",
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=["openid", "profile", "email"],
        authorization_uri="https://accounts.google.com/o/oauth2/v2/auth",
        token_uri="https://oauth2.googleapis.com/token",
        user_info_uri="https://www.googleapis.com/oauth2/v3/userinfo",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        issuer_uri="https://accounts.google.com",
        provider_name="Google",
        authorization_grant_type="authorization_code",
    )


def github(
    client_id: str,
    client_secret: str,
    redirect_uri: str = "",
) -> ClientRegistration:
    """Create a ClientRegistration pre-configured for GitHub OAuth2."""
    return ClientRegistration(
        registration_id="github",
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=["read:user", "user:email"],
        authorization_uri="https://github.com/login/oauth/authorize",
        token_uri="https://github.com/login/oauth/access_token",
        user_info_uri="https://api.github.com/user",
        provider_name="GitHub",
        authorization_grant_type="authorization_code",
    )


def keycloak(
    client_id: str,
    client_secret: str,
    issuer_uri: str,
    redirect_uri: str = "",
) -> ClientRegistration:
    """Create a ClientRegistration pre-configured for Keycloak.

    Args:
        client_id: The OAuth2 client identifier.
        client_secret: The OAuth2 client secret.
        issuer_uri: The Keycloak realm URL (e.g.,
            ``"https://keycloak.example.com/realms/myrealm"``).
        redirect_uri: The redirect URI for the authorization code flow.
    """
    base = issuer_uri.rstrip("/")
    return ClientRegistration(
        registration_id="keycloak",
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=["openid", "profile", "email"],
        authorization_uri=f"{base}/protocol/openid-connect/auth",
        token_uri=f"{base}/protocol/openid-connect/token",
        user_info_uri=f"{base}/protocol/openid-connect/userinfo",
        jwks_uri=f"{base}/protocol/openid-connect/certs",
        issuer_uri=issuer_uri,
        provider_name="Keycloak",
        authorization_grant_type="authorization_code",
    )


# ---------------------------------------------------------------------------
# Repository port and in-memory adapter
# ---------------------------------------------------------------------------


class ClientRegistrationRepository(Protocol):
    """Port for retrieving OAuth2 client registrations."""

    def find_by_registration_id(self, registration_id: str) -> ClientRegistration | None: ...


class InMemoryClientRegistrationRepository:
    """In-memory client registration repository.

    Stores registrations in a dict keyed by registration_id.
    """

    def __init__(self, *registrations: ClientRegistration) -> None:
        self._registrations: dict[str, ClientRegistration] = {r.registration_id: r for r in registrations}

    def find_by_registration_id(self, registration_id: str) -> ClientRegistration | None:
        return self._registrations.get(registration_id)

    def add(self, registration: ClientRegistration) -> None:
        self._registrations[registration.registration_id] = registration

    @property
    def registrations(self) -> list[ClientRegistration]:
        return list(self._registrations.values())
