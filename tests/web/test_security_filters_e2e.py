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
"""End-to-end security filter tests.

Tests both ``OAuth2ResourceServerFilter`` and ``HttpSecurityFilter`` through
a real Starlette app wired with ``WebFilterChainMiddleware``.

OAuth2ResourceServerFilter
--------------------------
The filter calls ``token_validator.to_security_context(token)`` on the Bearer
token. We inject a stub ``JWKSTokenValidator`` subclass that accepts a known
"valid" token string and raises ``SecurityException`` for everything else —
no actual JWT crypto needed.

HttpSecurityFilter
------------------
Built via the ``HttpSecurity`` DSL builder.  The security context is set on
``request.state.security_context`` by an upstream filter (here we use a
simple ``_InjectContextFilter`` helper to pre-populate it, mirroring what the
OAuth2 filter does in production).
"""

from __future__ import annotations

from typing import cast

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.http_security import HttpSecurity
from pyfly.security.oauth2.resource_server import JWKSTokenValidator
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters.http_security_filter import HttpSecurityFilter
from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "valid-test-token-abc123"
_INVALID_TOKEN = "invalid-token-xyz"


# ---------------------------------------------------------------------------
# Stub token validator
# ---------------------------------------------------------------------------


class _StubTokenValidator(JWKSTokenValidator):
    """Test double for JWKSTokenValidator.

    Accepts ``_VALID_TOKEN`` and returns a SecurityContext with user_id="user1"
    and roles=["USER"].  Raises ``SecurityException`` for any other token.
    Does not hit any network endpoint.
    """

    def __init__(self) -> None:
        # Do NOT call super().__init__() — it would try to connect to a JWKS URI.
        pass  # noqa: PIE790

    def to_security_context(self, token: str) -> SecurityContext:
        if token == _VALID_TOKEN:
            return SecurityContext(user_id="user1", roles=["USER"])
        raise SecurityException("Invalid token in stub", code="INVALID_TOKEN")


# ---------------------------------------------------------------------------
# Helper filter: pre-injects a SecurityContext onto request.state
# ---------------------------------------------------------------------------


class _InjectContextFilter(OncePerRequestFilter):
    """Injects a pre-built SecurityContext onto ``request.state.security_context``.

    Used in HttpSecurityFilter tests to simulate an upstream authentication
    filter having already set the security context (without needing a Bearer
    token round-trip).
    """

    def __init__(self, security_context: SecurityContext) -> None:
        self._ctx = security_context

    async def do_filter(self, request: Request, call_next: CallNext) -> Response:
        request.state.security_context = self._ctx
        return cast(Response, await call_next(request))


# ---------------------------------------------------------------------------
# App helpers
# ---------------------------------------------------------------------------


async def _protected_handler(request: Request) -> PlainTextResponse:
    return PlainTextResponse("secret data")


async def _public_handler(request: Request) -> PlainTextResponse:
    return PlainTextResponse("public data")


def _make_oauth2_app(token_validator: JWKSTokenValidator) -> Starlette:
    """App with OAuth2ResourceServerFilter protecting ``/api/*``."""
    oauth2_filter = OAuth2ResourceServerFilter(token_validator=token_validator)
    return Starlette(
        routes=[
            Route("/api/resource", _protected_handler),
            Route("/public", _public_handler),
        ],
        middleware=[Middleware(WebFilterChainMiddleware, filters=[oauth2_filter])],
    )


def _make_http_security_app(*filters: OncePerRequestFilter) -> Starlette:
    """App whose route handler echoes back the authenticated user or 'anon'."""

    async def _whoami_handler(request: Request) -> PlainTextResponse:
        ctx: SecurityContext = getattr(request.state, "security_context", SecurityContext.anonymous())
        return PlainTextResponse(ctx.user_id or "anon")

    return Starlette(
        routes=[
            Route("/api/admin", _protected_handler),
            Route("/api/user", _whoami_handler),
            Route("/public", _public_handler),
        ],
        middleware=[Middleware(WebFilterChainMiddleware, filters=list(filters))],
    )


# ---------------------------------------------------------------------------
# OAuth2ResourceServerFilter tests
# ---------------------------------------------------------------------------


class TestOAuth2ResourceServerFilter:
    """OAuth2ResourceServerFilter: Bearer-token validation gate."""

    def test_no_bearer_token_sets_anonymous_context(self) -> None:
        """Missing Authorization header → anonymous context; downstream runs normally."""
        validator = _StubTokenValidator()
        app = _make_oauth2_app(validator)
        client = TestClient(app)

        # The filter itself does NOT block — it just sets an anonymous context.
        # Blocking is done by HttpSecurityFilter; OAuth2 filter is authentication only.
        resp = client.get("/api/resource")
        assert resp.status_code == 200
        assert resp.text == "secret data"

    def test_valid_bearer_token_allows_request(self) -> None:
        """Valid Bearer token → security context populated; downstream succeeds."""
        validator = _StubTokenValidator()
        app = _make_oauth2_app(validator)
        client = TestClient(app)

        resp = client.get("/api/resource", headers={"Authorization": f"Bearer {_VALID_TOKEN}"})
        assert resp.status_code == 200
        assert resp.text == "secret data"

    def test_invalid_bearer_token_sets_anonymous_context(self) -> None:
        """Invalid token → SecurityException caught → anonymous context; request still proceeds.

        OAuth2ResourceServerFilter never blocks by itself — it sets the context
        to anonymous on bad tokens and lets downstream filters (HttpSecurityFilter)
        decide whether to reject.
        """
        validator = _StubTokenValidator()
        app = _make_oauth2_app(validator)
        client = TestClient(app)

        resp = client.get("/api/resource", headers={"Authorization": f"Bearer {_INVALID_TOKEN}"})
        # The filter does not short-circuit; it passes through with anonymous context.
        assert resp.status_code == 200

    def test_public_route_accessible_without_token(self) -> None:
        """Public endpoint reachable with no auth header at all."""
        validator = _StubTokenValidator()
        app = _make_oauth2_app(validator)
        client = TestClient(app)

        resp = client.get("/public")
        assert resp.status_code == 200
        assert resp.text == "public data"

    def test_security_context_populated_for_valid_token(self) -> None:
        """Verify the security context injected onto request.state has expected user."""
        validator = _StubTokenValidator()

        captured_ctx: list[SecurityContext] = []

        async def _capture_handler(request: Request) -> PlainTextResponse:
            ctx: SecurityContext = getattr(request.state, "security_context", SecurityContext.anonymous())
            captured_ctx.append(ctx)
            return PlainTextResponse("ok")

        from starlette.applications import Starlette as _Starlette
        from starlette.middleware import Middleware as _Middleware
        from starlette.routing import Route as _Route

        oauth2_filter = OAuth2ResourceServerFilter(token_validator=validator)
        app = _Starlette(
            routes=[_Route("/api/me", _capture_handler)],
            middleware=[_Middleware(WebFilterChainMiddleware, filters=[oauth2_filter])],
        )
        client = TestClient(app)
        client.get("/api/me", headers={"Authorization": f"Bearer {_VALID_TOKEN}"})

        assert len(captured_ctx) == 1
        ctx = captured_ctx[0]
        assert ctx.is_authenticated
        assert ctx.user_id == "user1"
        assert "USER" in ctx.roles

    def test_security_context_anonymous_for_missing_token(self) -> None:
        """No token → anonymous context (user_id=None, not authenticated)."""
        validator = _StubTokenValidator()

        captured_ctx: list[SecurityContext] = []

        async def _capture_handler(request: Request) -> PlainTextResponse:
            ctx: SecurityContext = getattr(request.state, "security_context", SecurityContext.anonymous())
            captured_ctx.append(ctx)
            return PlainTextResponse("ok")

        from starlette.applications import Starlette as _Starlette
        from starlette.middleware import Middleware as _Middleware
        from starlette.routing import Route as _Route

        oauth2_filter = OAuth2ResourceServerFilter(token_validator=validator)
        app = _Starlette(
            routes=[_Route("/api/me", _capture_handler)],
            middleware=[_Middleware(WebFilterChainMiddleware, filters=[oauth2_filter])],
        )
        client = TestClient(app)
        client.get("/api/me")

        assert len(captured_ctx) == 1
        ctx = captured_ctx[0]
        assert not ctx.is_authenticated
        assert ctx.user_id is None


# ---------------------------------------------------------------------------
# HttpSecurityFilter tests
# ---------------------------------------------------------------------------


class TestHttpSecurityFilter:
    """HttpSecurityFilter: URL-pattern authorization enforcement."""

    # -- unauthenticated requests --

    def test_authenticated_route_returns_401_when_anonymous(self) -> None:
        """Anonymous context on a route requiring authentication → 401."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/*").authenticated()
        security_filter = http_security.build()

        anonymous_ctx = SecurityContext.anonymous()
        inject_filter = _InjectContextFilter(anonymous_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/user")
        assert resp.status_code == 401
        body = resp.json()
        assert body["status"] == 401
        assert body["title"] == "Unauthorized"

    def test_role_route_returns_401_when_anonymous(self) -> None:
        """Anonymous context on a role-protected route → 401 (not authenticated at all)."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_role("ADMIN")
        security_filter = http_security.build()

        anonymous_ctx = SecurityContext.anonymous()
        inject_filter = _InjectContextFilter(anonymous_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 401

    # -- under-privileged authenticated requests --

    def test_role_route_returns_403_when_wrong_role(self) -> None:
        """Authenticated user without the required role → 403."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_role("ADMIN")
        security_filter = http_security.build()

        user_ctx = SecurityContext(user_id="user1", roles=["USER"])
        inject_filter = _InjectContextFilter(user_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == 403
        assert body["title"] == "Forbidden"
        assert "ADMIN" in body["detail"]

    def test_any_role_route_returns_403_when_no_matching_role(self) -> None:
        """User without any of the required roles → 403."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_any_role(["ADMIN", "SUPERUSER"])
        security_filter = http_security.build()

        user_ctx = SecurityContext(user_id="user1", roles=["USER"])
        inject_filter = _InjectContextFilter(user_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 403

    # -- authorized requests (pass-through) --

    def test_authenticated_route_allows_authenticated_user(self) -> None:
        """Authenticated context on an ``authenticated()`` rule → 200."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/*").authenticated().any_request().permit_all()
        security_filter = http_security.build()

        user_ctx = SecurityContext(user_id="user1", roles=["USER"])
        inject_filter = _InjectContextFilter(user_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/user")
        assert resp.status_code == 200

    def test_role_route_allows_user_with_correct_role(self) -> None:
        """User with the correct role on an ``has_role()`` rule → 200."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_role("ADMIN").any_request().permit_all()
        security_filter = http_security.build()

        admin_ctx = SecurityContext(user_id="admin1", roles=["ADMIN"])
        inject_filter = _InjectContextFilter(admin_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 200

    def test_any_role_route_allows_user_with_one_matching_role(self) -> None:
        """User satisfying ``has_any_role`` → 200."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_any_role(
            ["ADMIN", "SUPERUSER"]
        ).any_request().permit_all()
        security_filter = http_security.build()

        admin_ctx = SecurityContext(user_id="admin1", roles=["SUPERUSER"])
        inject_filter = _InjectContextFilter(admin_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 200

    # -- permit_all and deny_all --

    def test_permit_all_allows_any_request(self) -> None:
        """``permit_all()`` passes anonymous requests without challenge."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/public").permit_all()
        security_filter = http_security.build()

        anonymous_ctx = SecurityContext.anonymous()
        inject_filter = _InjectContextFilter(anonymous_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/public")
        assert resp.status_code == 200
        assert resp.text == "public data"

    def test_deny_all_blocks_authenticated_user(self) -> None:
        """``deny_all()`` returns 403 even for authenticated users."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").deny_all()
        security_filter = http_security.build()

        admin_ctx = SecurityContext(user_id="admin1", roles=["ADMIN"])
        inject_filter = _InjectContextFilter(admin_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.status_code == 403

    # -- deny-by-default when rules are configured --

    def test_unmatched_path_denied_by_default(self) -> None:
        """When rules exist but no rule matches the path, the request is denied (fail-closed)."""
        http_security = HttpSecurity()
        # Only rule: admin paths require ADMIN role; /api/user is unmatched → denied
        http_security.authorize_requests().request_matchers("/api/admin").has_role("ADMIN")
        security_filter = http_security.build()

        admin_ctx = SecurityContext(user_id="admin1", roles=["ADMIN"])
        inject_filter = _InjectContextFilter(admin_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        # /api/user is not covered by any rule → denied by default
        resp = client.get("/api/user")
        assert resp.status_code == 403

    def test_no_rules_is_noop(self) -> None:
        """An HttpSecurity with no rules at all is a no-op (never a blanket lockout)."""
        security_filter = HttpSecurityFilter(rules=[])

        anonymous_ctx = SecurityContext.anonymous()
        inject_filter = _InjectContextFilter(anonymous_ctx)

        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        # Everything passes through because no rules are configured
        resp = client.get("/api/admin")
        assert resp.status_code == 200

    # -- problem detail format --

    def test_401_response_is_rfc7807_problem_detail(self) -> None:
        """401 response follows RFC 7807 problem-detail structure."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/*").authenticated()
        security_filter = http_security.build()

        inject_filter = _InjectContextFilter(SecurityContext.anonymous())
        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/user")
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert set(body.keys()) >= {"type", "title", "status", "detail", "instance"}
        assert body["instance"] == "/api/user"

    def test_403_response_is_rfc7807_problem_detail(self) -> None:
        """403 response follows RFC 7807 problem-detail structure."""
        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/admin").has_role("ADMIN")
        security_filter = http_security.build()

        user_ctx = SecurityContext(user_id="user1", roles=["USER"])
        inject_filter = _InjectContextFilter(user_ctx)
        app = _make_http_security_app(inject_filter, security_filter)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin")
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert set(body.keys()) >= {"type", "title", "status", "detail", "instance"}
        assert body["instance"] == "/api/admin"


# ---------------------------------------------------------------------------
# Combined OAuth2 + HttpSecurity pipeline
# ---------------------------------------------------------------------------


class TestOAuth2AndHttpSecurityPipeline:
    """Full pipeline: OAuth2ResourceServerFilter → HttpSecurityFilter → handler."""

    def _make_pipeline_app(self) -> Starlette:
        validator = _StubTokenValidator()
        oauth2_filter = OAuth2ResourceServerFilter(token_validator=validator)

        http_security = HttpSecurity()
        http_security.authorize_requests().request_matchers("/api/*").authenticated().request_matchers(
            "/public"
        ).permit_all().any_request().permit_all()
        security_filter = http_security.build()

        async def _handler(request: Request) -> PlainTextResponse:
            ctx: SecurityContext = getattr(request.state, "security_context", SecurityContext.anonymous())
            return PlainTextResponse(ctx.user_id or "anon")

        return Starlette(
            routes=[
                Route("/api/resource", _handler),
                Route("/public", _handler),
            ],
            middleware=[Middleware(WebFilterChainMiddleware, filters=[oauth2_filter, security_filter])],
        )

    def test_unauthenticated_api_request_blocked(self) -> None:
        """No token → anonymous context → HttpSecurityFilter returns 401."""
        app = self._make_pipeline_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/resource")
        assert resp.status_code == 401

    def test_valid_token_api_request_succeeds(self) -> None:
        """Valid token → authenticated context → HttpSecurityFilter passes through."""
        app = self._make_pipeline_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/resource", headers={"Authorization": f"Bearer {_VALID_TOKEN}"})
        assert resp.status_code == 200
        assert resp.text == "user1"

    def test_invalid_token_api_request_blocked(self) -> None:
        """Invalid token → anonymous context → HttpSecurityFilter returns 401."""
        app = self._make_pipeline_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/resource", headers={"Authorization": f"Bearer {_INVALID_TOKEN}"})
        assert resp.status_code == 401

    def test_public_route_accessible_without_token(self) -> None:
        """Public route allowed by ``permit_all()`` even for anonymous requests."""
        app = self._make_pipeline_app()
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/public")
        assert resp.status_code == 200
        assert resp.text == "anon"
