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
"""RFC 9207 ``iss`` authorization-response validation (mix-up defense)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.login import _OAUTH2_STATE_KEY, OAuth2LoginHandler
from pyfly.session.session import HttpSession


def _handler(**reg_overrides: Any) -> OAuth2LoginHandler:
    base: dict[str, Any] = dict(
        registration_id="acme",
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://app/cb",
        scopes=["openid"],
        authorization_uri="https://idp/auth",
        token_uri="https://idp/token",
        issuer_uri="https://good.example.com",
        use_pkce=False,
    )
    base.update(reg_overrides)
    return OAuth2LoginHandler(InMemoryClientRegistrationRepository(ClientRegistration(**base)))


def _callback(query: str, *, state: str | None = "st") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/login/oauth2/code/acme",
        "headers": [],
        "query_string": query.encode(),
        "path_params": {"registration_id": "acme"},
    }
    request = Request(scope)
    session = HttpSession("sid", {})
    if state is not None:
        session.set_attribute(_OAUTH2_STATE_KEY, state)
    request.state.session = session
    return request


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(bytes(resp.body).decode("utf-8"))


@pytest.mark.asyncio
async def test_callback_aborts_on_iss_mismatch() -> None:
    """A returned iss that differs from the registration's issuer aborts (mix-up)."""
    handler = _handler()
    resp = await handler._handle_callback(_callback("state=st&code=abc&iss=https://evil.example.com"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_iss"


@pytest.mark.asyncio
async def test_callback_requires_iss_when_configured() -> None:
    """With require_iss=True, a callback lacking the iss param is rejected."""
    handler = _handler(require_iss=True)
    resp = await handler._handle_callback(_callback("state=st&code=abc"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_iss"


@pytest.mark.asyncio
async def test_callback_iss_match_passes_to_token_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching iss passes validation and proceeds to the token exchange."""
    handler = _handler(require_iss=True)

    async def _fake_exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}  # empty -> handler returns 502 token_exchange_failed (proves we got past iss)

    monkeypatch.setattr(handler, "_exchange_code", _fake_exchange)
    resp = await handler._handle_callback(_callback("state=st&code=abc&iss=https://good.example.com"))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_callback_no_iss_param_allowed_when_not_required() -> None:
    """Default (require_iss=False): a missing iss param does not block the flow."""
    handler = _handler()

    async def _fake_exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    monkeypatch_done = False

    async def _patched(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal monkeypatch_done
        monkeypatch_done = True
        return {}

    handler._exchange_code = _patched  # type: ignore[assignment]
    resp = await handler._handle_callback(_callback("state=st&code=abc"))
    assert resp.status_code == 502
    assert monkeypatch_done
