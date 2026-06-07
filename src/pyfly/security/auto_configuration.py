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
"""Security auto-configuration — JWT, password encoding, and OAuth2 beans."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

try:
    from pyfly.security.jwt import JWTService
except ImportError:
    JWTService = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.password import BcryptPasswordEncoder
except ImportError:
    BcryptPasswordEncoder = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.resource_server import JWKSTokenValidator
except ImportError:
    JWKSTokenValidator = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
except ImportError:
    AuthorizationServer = object  # type: ignore[misc,assignment]
    InMemoryTokenStore = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.client import (
        ClientRegistration,
        InMemoryClientRegistrationRepository,
    )
except ImportError:
    ClientRegistration = object  # type: ignore[misc,assignment]
    InMemoryClientRegistrationRepository = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.login import OAuth2LoginHandler
except ImportError:
    OAuth2LoginHandler = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.session_security_filter import OAuth2SessionSecurityFilter
except ImportError:
    OAuth2SessionSecurityFilter = object  # type: ignore[misc,assignment]

try:
    from pyfly.web.ports.filter import WebFilter
except ImportError:
    WebFilter = object  # type: ignore[misc,assignment]

from collections.abc import Sequence
from typing import Any

from pyfly.config.auto import AutoConfiguration
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError, NoUniqueBeanError
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config


def _exclude_patterns(config: Config, key: str) -> Sequence[str]:
    """Read a list / comma-separated string of exclude path patterns from config."""
    raw = config.get(key)
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


@auto_configuration
@conditional_on_property("pyfly.security.enabled", having_value="true")
@conditional_on_class("jwt")
class JwtAutoConfiguration:
    """Auto-configures a JWTService bean when pyjwt is installed."""

    @bean
    def jwt_service(self, config: Config) -> JWTService:
        secret = str(config.get("pyfly.security.jwt.secret", "change-me-in-production"))
        algorithm = str(config.get("pyfly.security.jwt.algorithm", "HS256"))
        return JWTService(secret=secret, algorithm=algorithm)

    @bean
    @conditional_on_property("pyfly.security.jwt.filter.enabled", having_value="true")
    @conditional_on_class("starlette")
    def security_filter(self, jwt_service: JWTService, config: Config) -> WebFilter:
        # Symmetric-JWT authentication filter (opt-in). Registered as a bean so
        # the post-start filter rescan in create_app adds it to the chain (#41).
        from pyfly.web.adapters.starlette.filters.security_filter import SecurityFilter

        return SecurityFilter(
            jwt_service=jwt_service,
            exclude_patterns=_exclude_patterns(config, "pyfly.security.jwt.exclude-patterns"),
        )


@auto_configuration
@conditional_on_property("pyfly.security.enabled", having_value="true")
@conditional_on_class("bcrypt")
class PasswordEncoderAutoConfiguration:
    """Auto-configures a BcryptPasswordEncoder bean when bcrypt is installed."""

    @bean
    def password_encoder(self, config: Config) -> BcryptPasswordEncoder:
        rounds = int(config.get("pyfly.security.password.bcrypt-rounds", 12))
        return BcryptPasswordEncoder(rounds=rounds)


# ---------------------------------------------------------------------------
# OAuth2 Resource Server
# ---------------------------------------------------------------------------


@auto_configuration
@conditional_on_property("pyfly.security.oauth2.resource-server.enabled", having_value="true")
@conditional_on_class("jwt")
class OAuth2ResourceServerAutoConfiguration:
    """Auto-configures a JWKSTokenValidator when a JWKS URI is provided.

    Activated when ``pyfly.security.oauth2.resource-server.enabled=true``
    and ``pyjwt`` is installed.  Reads the JWKS endpoint, issuer, and
    audience from configuration properties.
    """

    @bean
    @conditional_on_missing_bean(JWKSTokenValidator)
    def jwks_token_validator(self, config: Config) -> JWKSTokenValidator:
        jwks_uri = str(config.get("pyfly.security.oauth2.resource-server.jwks-uri", ""))
        issuer = config.get("pyfly.security.oauth2.resource-server.issuer")
        audience = config.get("pyfly.security.oauth2.resource-server.audience")
        return JWKSTokenValidator(
            jwks_uri=jwks_uri,
            issuer=str(issuer) if issuer is not None else None,
            audience=str(audience) if audience is not None else None,
        )

    @bean
    @conditional_on_class("starlette")
    def oauth2_resource_server_filter(self, token_validator: JWKSTokenValidator, config: Config) -> WebFilter:
        # Bearer-token resource-server filter; registered so the post-start
        # rescan adds it to the chain whenever the resource server is on (#41).
        from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter

        return OAuth2ResourceServerFilter(
            token_validator=token_validator,
            exclude_patterns=_exclude_patterns(config, "pyfly.security.oauth2.resource-server.exclude-patterns"),
        )


# ---------------------------------------------------------------------------
# OAuth2 Authorization Server
# ---------------------------------------------------------------------------


@auto_configuration
@conditional_on_property("pyfly.security.oauth2.authorization-server.enabled", having_value="true")
@conditional_on_class("jwt")
class OAuth2AuthorizationServerAutoConfiguration:
    """Auto-configures an AuthorizationServer for issuing JWT access tokens.

    Activated when ``pyfly.security.oauth2.authorization-server.enabled=true``
    and ``pyjwt`` is installed.  Uses the configured client repository and
    an in-memory token store by default.
    """

    @bean
    @conditional_on_missing_bean(AuthorizationServer)
    def authorization_server(
        self,
        config: Config,
        client_registration_repository: InMemoryClientRegistrationRepository,
        container: Container,
    ) -> AuthorizationServer:
        secret = str(config.get("pyfly.security.oauth2.authorization-server.secret", "change-me-in-production"))
        issuer = config.get("pyfly.security.oauth2.authorization-server.issuer")
        access_ttl = int(config.get("pyfly.security.oauth2.authorization-server.access-token-ttl", 3600))
        refresh_ttl = int(config.get("pyfly.security.oauth2.authorization-server.refresh-token-ttl", 86400))
        token_store = self._build_token_store(config, container, refresh_ttl)
        return AuthorizationServer(
            secret=secret,
            client_repository=client_registration_repository,
            token_store=token_store,
            access_token_ttl=access_ttl,
            refresh_token_ttl=refresh_ttl,
            issuer=str(issuer) if issuer is not None else None,
        )

    def _build_token_store(self, config: Config, container: Container, refresh_ttl: int) -> Any:
        """Select the token-store backend (Spring parity for a persistent authorization server).

        ``pyfly.security.oauth2.token-store.provider``: ``memory`` (default, single-instance),
        ``redis`` (fast cross-instance revocation, TTL = refresh-token lifetime), or ``postgres``
        (durable + auditable). The Redis client / SQLAlchemy engine are obtained here (the
        composition root) and injected — the adapters never import their driver at module scope.
        """
        provider = str(config.get("pyfly.security.oauth2.token-store.provider", "memory")).lower()
        if provider == "redis" and AutoConfiguration.is_available("redis.asyncio"):
            import redis.asyncio as aioredis

            from pyfly.security.adapters.redis_token_store import RedisTokenStore

            url = str(
                config.get("pyfly.security.oauth2.token-store.redis.url")
                or config.get("pyfly.session.redis.url", "redis://localhost:6379/0")
            )
            return RedisTokenStore(aioredis.from_url(url), ttl=refresh_ttl)  # type: ignore[no-untyped-call,unused-ignore]
        if provider == "postgres":
            from pyfly.security.adapters.postgres_token_store import PostgresTokenStore

            def _engine() -> Any:
                from sqlalchemy.ext.asyncio import AsyncEngine

                return container.resolve(AsyncEngine)

            return PostgresTokenStore(_engine)
        return InMemoryTokenStore()


# ---------------------------------------------------------------------------
# OAuth2 Client
# ---------------------------------------------------------------------------


@auto_configuration
@conditional_on_property("pyfly.security.oauth2.client.enabled", having_value="true")
class OAuth2ClientAutoConfiguration:
    """Auto-configures an InMemoryClientRegistrationRepository from config.

    Activated when ``pyfly.security.oauth2.client.enabled=true``.  Reads
    client registrations from ``pyfly.security.oauth2.client.registrations``
    configuration properties.

    Each registration is a dict with keys matching
    :class:`~pyfly.security.oauth2.client.ClientRegistration` fields::

        pyfly:
          security:
            oauth2:
              client:
                enabled: true
                registrations:
                  my-app:
                    client-id: "abc123"
                    client-secret: "secret"
                    authorization-grant-type: "authorization_code"
                    scopes: "openid,profile,email"
                    token-uri: "https://provider.example.com/oauth/token"
    """

    @bean
    @conditional_on_missing_bean(InMemoryClientRegistrationRepository)
    def client_registration_repository(self, config: Config) -> InMemoryClientRegistrationRepository:
        raw = config.get("pyfly.security.oauth2.client.registrations", {})
        registrations: list[ClientRegistration] = []

        if isinstance(raw, dict):
            for reg_id, props in raw.items():
                if not isinstance(props, dict):
                    continue
                scopes_value = props.get("scopes", "")
                if isinstance(scopes_value, str):
                    scopes = [s.strip() for s in scopes_value.split(",") if s.strip()]
                elif isinstance(scopes_value, list):
                    scopes = list(scopes_value)
                else:
                    scopes = []

                registrations.append(
                    ClientRegistration(
                        registration_id=str(reg_id),
                        client_id=str(props.get("client-id", "")),
                        client_secret=str(props.get("client-secret", "")),
                        authorization_grant_type=str(props.get("authorization-grant-type", "authorization_code")),
                        redirect_uri=str(props.get("redirect-uri", "")),
                        scopes=scopes,
                        authorization_uri=str(props.get("authorization-uri", "")),
                        token_uri=str(props.get("token-uri", "")),
                        user_info_uri=str(props.get("user-info-uri", "")),
                        jwks_uri=str(props.get("jwks-uri", "")),
                        issuer_uri=str(props.get("issuer-uri", "")),
                        provider_name=str(props.get("provider-name", "")),
                    )
                )

        return InMemoryClientRegistrationRepository(*registrations)


# ---------------------------------------------------------------------------
# OAuth2 Login (authorization_code flow)
# ---------------------------------------------------------------------------


@auto_configuration
@conditional_on_property("pyfly.security.oauth2.login.enabled", having_value="true")
class OAuth2LoginAutoConfiguration:
    """Auto-configures OAuth2LoginHandler and OAuth2SessionSecurityFilter.

    Activated when ``pyfly.security.oauth2.login.enabled=true``.  Requires
    an ``InMemoryClientRegistrationRepository`` bean (typically provided by
    ``OAuth2ClientAutoConfiguration``).
    """

    @bean
    @conditional_on_missing_bean(OAuth2LoginHandler)
    def oauth2_login_handler(
        self,
        client_registration_repository: InMemoryClientRegistrationRepository,
        container: Container,
    ) -> OAuth2LoginHandler:
        # Wire session concurrency control if a controller bean is present (opt-in via
        # pyfly.session.concurrency.enabled); otherwise the handler enforces no cap.
        from pyfly.session.concurrency import SessionConcurrencyController

        try:
            concurrency = container.resolve(SessionConcurrencyController)
        except (NoSuchBeanError, NoUniqueBeanError):
            concurrency = None
        return OAuth2LoginHandler(client_repository=client_registration_repository, concurrency=concurrency)

    @bean
    @conditional_on_missing_bean(OAuth2SessionSecurityFilter)
    def oauth2_session_security_filter(self) -> OAuth2SessionSecurityFilter:
        return OAuth2SessionSecurityFilter()
