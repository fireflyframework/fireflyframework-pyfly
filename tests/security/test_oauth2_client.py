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
"""Tests for the OAuth2 Client Registration model and repository."""

from __future__ import annotations

import dataclasses

import pytest

from pyfly.security.oauth2.client import (
    ClientRegistration,
    InMemoryClientRegistrationRepository,
    github,
    google,
    keycloak,
)

# ---------------------------------------------------------------------------
# ClientRegistration dataclass
# ---------------------------------------------------------------------------


class TestClientRegistration:
    """Tests for :class:`ClientRegistration`."""

    def test_client_registration_creation(self) -> None:
        """Creating a ClientRegistration with all fields stores each value."""
        reg = ClientRegistration(
            registration_id="my-provider",
            client_id="my-client-id",
            client_secret="my-secret",
            authorization_grant_type="authorization_code",
            redirect_uri="https://app.example.com/callback",
            scopes=["openid", "profile"],
            authorization_uri="https://auth.example.com/authorize",
            token_uri="https://auth.example.com/token",
            user_info_uri="https://auth.example.com/userinfo",
            jwks_uri="https://auth.example.com/.well-known/jwks.json",
            issuer_uri="https://auth.example.com",
            provider_name="MyProvider",
        )

        assert reg.registration_id == "my-provider"
        assert reg.client_id == "my-client-id"
        assert reg.client_secret == "my-secret"
        assert reg.authorization_grant_type == "authorization_code"
        assert reg.redirect_uri == "https://app.example.com/callback"
        assert reg.scopes == ["openid", "profile"]
        assert reg.authorization_uri == "https://auth.example.com/authorize"
        assert reg.token_uri == "https://auth.example.com/token"
        assert reg.user_info_uri == "https://auth.example.com/userinfo"
        assert reg.jwks_uri == "https://auth.example.com/.well-known/jwks.json"
        assert reg.issuer_uri == "https://auth.example.com"
        assert reg.provider_name == "MyProvider"

    def test_client_registration_defaults(self) -> None:
        """Only registration_id and client_id are required; others have defaults."""
        reg = ClientRegistration(
            registration_id="minimal",
            client_id="cid",
        )

        assert reg.registration_id == "minimal"
        assert reg.client_id == "cid"
        assert reg.client_secret == ""
        assert reg.authorization_grant_type == "authorization_code"
        assert reg.redirect_uri == ""
        assert reg.scopes == []
        assert reg.authorization_uri == ""
        assert reg.token_uri == ""
        assert reg.user_info_uri == ""
        assert reg.jwks_uri == ""
        assert reg.issuer_uri == ""
        assert reg.provider_name == ""

    def test_client_registration_is_frozen(self) -> None:
        """Frozen dataclass prevents attribute mutation after creation."""
        reg = ClientRegistration(registration_id="frozen", client_id="cid")

        with pytest.raises(dataclasses.FrozenInstanceError):
            reg.client_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Built-in provider factories
# ---------------------------------------------------------------------------


class TestProviderFactories:
    """Tests for the built-in provider factory functions."""

    def test_google_provider(self) -> None:
        """google() returns a correctly pre-configured ClientRegistration."""
        reg = google("gid", "gsecret")

        assert reg.registration_id == "google"
        assert reg.client_id == "gid"
        assert reg.client_secret == "gsecret"
        assert reg.authorization_grant_type == "authorization_code"
        assert reg.redirect_uri == ""
        assert reg.scopes == ["openid", "profile", "email"]
        assert reg.authorization_uri == "https://accounts.google.com/o/oauth2/v2/auth"
        assert reg.token_uri == "https://oauth2.googleapis.com/token"
        assert reg.user_info_uri == "https://www.googleapis.com/oauth2/v3/userinfo"
        assert reg.jwks_uri == "https://www.googleapis.com/oauth2/v3/certs"
        assert reg.issuer_uri == "https://accounts.google.com"
        assert reg.provider_name == "Google"

    def test_github_provider(self) -> None:
        """github() returns a correctly pre-configured ClientRegistration."""
        reg = github("ghid", "ghsecret")

        assert reg.registration_id == "github"
        assert reg.client_id == "ghid"
        assert reg.client_secret == "ghsecret"
        assert reg.authorization_grant_type == "authorization_code"
        assert reg.redirect_uri == ""
        assert reg.scopes == ["read:user", "user:email"]
        assert reg.authorization_uri == "https://github.com/login/oauth/authorize"
        assert reg.token_uri == "https://github.com/login/oauth/access_token"
        assert reg.user_info_uri == "https://api.github.com/user"
        assert reg.jwks_uri == ""
        assert reg.provider_name == "GitHub"

    def test_keycloak_provider(self) -> None:
        """keycloak() derives OIDC URIs from the issuer_uri."""
        reg = keycloak("kcid", "kcsecret", "https://kc.example.com/realms/test")

        assert reg.registration_id == "keycloak"
        assert reg.client_id == "kcid"
        assert reg.client_secret == "kcsecret"
        assert reg.authorization_grant_type == "authorization_code"
        assert reg.redirect_uri == ""
        assert reg.scopes == ["openid", "profile", "email"]
        assert reg.issuer_uri == "https://kc.example.com/realms/test"
        assert reg.provider_name == "Keycloak"

        base = "https://kc.example.com/realms/test"
        assert reg.authorization_uri == f"{base}/protocol/openid-connect/auth"
        assert reg.token_uri == f"{base}/protocol/openid-connect/token"
        assert reg.user_info_uri == f"{base}/protocol/openid-connect/userinfo"
        assert reg.jwks_uri == f"{base}/protocol/openid-connect/certs"


# ---------------------------------------------------------------------------
# InMemoryClientRegistrationRepository
# ---------------------------------------------------------------------------


class TestInMemoryClientRegistrationRepository:
    """Tests for :class:`InMemoryClientRegistrationRepository`."""

    def test_find_by_registration_id(self) -> None:
        """Stored registrations can be retrieved by registration_id."""
        g = google("gid", "gs")
        gh = github("ghid", "ghs")
        repo = InMemoryClientRegistrationRepository(g, gh)

        assert repo.find_by_registration_id("google") is g
        assert repo.find_by_registration_id("github") is gh

    def test_find_by_registration_id_not_found(self) -> None:
        """Returns None for an unknown registration_id."""
        repo = InMemoryClientRegistrationRepository()

        assert repo.find_by_registration_id("nonexistent") is None

    def test_add(self) -> None:
        """Registrations can be added after construction."""
        repo = InMemoryClientRegistrationRepository()
        assert repo.find_by_registration_id("google") is None

        g = google("gid", "gs")
        repo.add(g)

        assert repo.find_by_registration_id("google") is g

    def test_registrations_property(self) -> None:
        """The registrations property returns all stored registrations."""
        g = google("gid", "gs")
        gh = github("ghid", "ghs")
        repo = InMemoryClientRegistrationRepository(g, gh)

        all_regs = repo.registrations
        assert len(all_regs) == 2
        assert set(r.registration_id for r in all_regs) == {"google", "github"}
