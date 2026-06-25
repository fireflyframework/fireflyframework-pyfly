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
"""OAuth2 authorization_code PKCE (v26.06.54)."""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import _OAUTH2_PKCE_VERIFIER_KEY, OAuth2LoginHandler, _generate_pkce
from pyfly.session.session import HttpSession


def _handler(*, use_pkce: bool) -> OAuth2LoginHandler:
    reg = ClientRegistration(
        registration_id="acme",
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://app/cb",
        scopes=["openid"],
        authorization_uri="https://idp/auth",
        token_uri="https://idp/token",
        use_pkce=use_pkce,
    )
    return OAuth2LoginHandler(InMemoryClientRegistrationRepository(reg))


def _reg(**overrides: Any) -> ClientRegistration:
    base: dict[str, Any] = dict(
        registration_id="acme",
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://app/cb",
        scopes=["openid"],
        authorization_uri="https://idp/auth",
        token_uri="https://idp/token",
    )
    base.update(overrides)
    return ClientRegistration(**base)


def _request(rid: str = "acme") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": f"/oauth2/authorization/{rid}",
        "headers": [],
        "query_string": b"",
        "path_params": {"registration_id": rid},
    }
    request = Request(scope)
    request.state.session = HttpSession("sid", {})
    return request


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def test_generate_pkce_is_valid_s256() -> None:
    verifier, challenge = _generate_pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == _s256(verifier)


@pytest.mark.asyncio
async def test_authorization_adds_pkce_challenge_when_enabled() -> None:
    request = _request()
    response = await _handler(use_pkce=True)._handle_authorization(request)
    query = parse_qs(urlparse(response.headers["location"]).query)

    assert query["code_challenge_method"] == ["S256"]
    verifier = request.state.session.get_attribute(_OAUTH2_PKCE_VERIFIER_KEY)
    assert verifier  # one-time verifier stashed in the session
    assert query["code_challenge"][0] == _s256(verifier)  # challenge matches the stashed verifier


@pytest.mark.asyncio
async def test_authorization_omits_pkce_when_disabled() -> None:
    request = _request()
    response = await _handler(use_pkce=False)._handle_authorization(request)
    query = parse_qs(urlparse(response.headers["location"]).query)

    assert "code_challenge" not in query
    assert request.state.session.get_attribute(_OAUTH2_PKCE_VERIFIER_KEY) is None


def test_pkce_enabled_by_default() -> None:
    """RFC 9700 / OAuth 2.1: PKCE is on by default for the authorization_code flow."""
    reg = ClientRegistration(registration_id="x", client_id="c")
    assert reg.use_pkce is True


@pytest.mark.asyncio
async def test_authorization_adds_pkce_by_default() -> None:
    """A registration that does not mention PKCE still gets a code_challenge."""
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(_reg()))
    request = _request()
    response = await handler._handle_authorization(request)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert request.state.session.get_attribute(_OAUTH2_PKCE_VERIFIER_KEY)


@pytest.mark.asyncio
async def test_public_client_forces_pkce_even_if_disabled() -> None:
    """A public client (no client_secret) gets PKCE even if it tries to opt out —
    it has no other defense against authorization-code injection."""
    handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(_reg(client_secret="", use_pkce=False)))
    request = _request()
    response = await handler._handle_authorization(request)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert request.state.session.get_attribute(_OAUTH2_PKCE_VERIFIER_KEY)


def test_client_autoconfig_enables_pkce_by_default() -> None:
    from pyfly.core.config import Config
    from pyfly.security.auto_configuration import OAuth2ClientAutoConfiguration

    cfg = Config(
        {
            "pyfly": {
                "security": {
                    "oauth2": {
                        "client": {
                            "enabled": "true",
                            "registrations": {"acme": {"client-id": "c", "token-uri": "https://idp/token"}},
                        }
                    }
                }
            }
        }
    )
    repo = OAuth2ClientAutoConfiguration().client_registration_repository(cfg)
    reg = repo.find_by_registration_id("acme")
    assert reg is not None and reg.use_pkce is True


def test_client_autoconfig_pkce_can_be_disabled() -> None:
    from pyfly.core.config import Config
    from pyfly.security.auto_configuration import OAuth2ClientAutoConfiguration

    cfg = Config(
        {
            "pyfly": {
                "security": {
                    "oauth2": {
                        "client": {
                            "enabled": "true",
                            "registrations": {"acme": {"client-id": "c", "client-secret": "s", "use-pkce": "false"}},
                        }
                    }
                }
            }
        }
    )
    repo = OAuth2ClientAutoConfiguration().client_registration_repository(cfg)
    reg = repo.find_by_registration_id("acme")
    assert reg is not None and reg.use_pkce is False


@pytest.mark.asyncio
async def test_exchange_code_sends_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"access_token": "AT"}

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, data: dict | None = None, headers: dict | None = None) -> _FakeResponse:
            captured["data"] = data
            return _FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    handler = _handler(use_pkce=True)
    reg = handler._client_repository.find_by_registration_id("acme")
    result = await handler._exchange_code(reg, "the-code", "the-verifier")

    assert result == {"access_token": "AT"}
    assert captured["data"]["code_verifier"] == "the-verifier"
    assert captured["data"]["grant_type"] == "authorization_code"
