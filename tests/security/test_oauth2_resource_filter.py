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
"""OAuth2ResourceServerFilter — request-chain behaviour.

Pins: authenticated context on a valid token, anonymous fall-through on
missing/invalid tokens (default), opt-in strict ``401`` rejection with an RFC
6750 ``WWW-Authenticate`` challenge, case-insensitive Bearer scheme, and
``exclude_patterns`` skipping.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import (
    ERROR_MODE_401,
    OAuth2ResourceServerFilter,
)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeValidator:
    """In-memory validator: a real RS256 verify against one key, no network.

    Stands in for JWKSTokenValidator so the filter tests stay hermetic and fast;
    the JWKS/HTTP path is covered in test_oauth2_resource_server.py.
    """

    def to_security_context(self, token: str) -> SecurityContext:
        try:
            payload = jwt.decode(token, _KEY.public_key(), algorithms=["RS256"], options={"require": ["exp"]})
        except jwt.PyJWTError as exc:
            raise SecurityException(f"bad token: {exc}", code="INVALID_TOKEN") from exc
        return SecurityContext(user_id=payload["sub"], roles=payload.get("roles", []))


def _token(sub: str = "u", roles: list[str] | None = None) -> str:
    return jwt.encode({"sub": sub, "roles": roles or [], "exp": int(time.time()) + 3600}, _KEY, algorithm="RS256")


def _request(path: str, auth: str | None) -> Request:
    headers = [(b"authorization", auth.encode())] if auth is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    return Request(scope)


async def _run(flt: OAuth2ResourceServerFilter, req: Request) -> tuple[Response, Any]:
    captured: dict[str, Any] = {}

    async def call_next(r: Request) -> Response:
        captured["ctx"] = getattr(r.state, "security_context", None)
        return PlainTextResponse("ok")

    resp = await flt.do_filter(req, call_next)
    return resp, captured.get("ctx")


@pytest.mark.asyncio
async def test_valid_token_sets_authenticated_context() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator())
    resp, ctx = await _run(flt, _request("/api/data", f"Bearer {_token('alice', ['admin'])}"))
    assert resp.status_code == 200
    assert ctx.is_authenticated and ctx.user_id == "alice" and ctx.roles == ["admin"]


@pytest.mark.asyncio
async def test_missing_token_is_anonymous_and_proceeds() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator())
    resp, ctx = await _run(flt, _request("/api/data", None))
    assert resp.status_code == 200
    assert ctx is not None and not ctx.is_authenticated


@pytest.mark.asyncio
async def test_invalid_token_anonymous_mode_proceeds() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator())  # default anonymous
    resp, ctx = await _run(flt, _request("/api/data", "Bearer not.a.jwt"))
    assert resp.status_code == 200
    assert ctx is not None and not ctx.is_authenticated


@pytest.mark.asyncio
async def test_invalid_token_401_mode_rejects_with_challenge() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator(), error_mode=ERROR_MODE_401)
    resp, ctx = await _run(flt, _request("/api/data", "Bearer not.a.jwt"))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert ctx is None  # call_next never reached


@pytest.mark.asyncio
async def test_missing_token_401_mode_still_falls_through() -> None:
    # Strict mode rejects only PRESENT-but-invalid tokens; a missing token still
    # falls through to the gate so public endpoints stay reachable.
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator(), error_mode=ERROR_MODE_401)
    resp, ctx = await _run(flt, _request("/public", None))
    assert resp.status_code == 200
    assert ctx is not None and not ctx.is_authenticated


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator())
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        _, ctx = await _run(flt, _request("/api/data", f"{scheme} {_token('bob')}"))
        assert ctx.is_authenticated and ctx.user_id == "bob", scheme


@pytest.mark.asyncio
async def test_non_bearer_scheme_is_ignored() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator())
    _, ctx = await _run(flt, _request("/api/data", "Basic dXNlcjpwYXNz"))
    assert ctx is not None and not ctx.is_authenticated


def test_exclude_patterns_skip_via_base_dispatch() -> None:
    flt = OAuth2ResourceServerFilter(
        token_validator=_FakeValidator(), exclude_patterns=["/actuator/*", "/api/v1/version"]
    )
    assert flt.should_not_filter(_request("/actuator/health", None)) is True
    assert flt.should_not_filter(_request("/actuator/health/liveness", None)) is True
    assert flt.should_not_filter(_request("/api/v1/version", None)) is True
    assert flt.should_not_filter(_request("/api/v1/data", None)) is False


def test_invalid_error_mode_falls_back_to_anonymous() -> None:
    flt = OAuth2ResourceServerFilter(token_validator=_FakeValidator(), error_mode="bogus")
    assert flt._error_mode == "anonymous"
