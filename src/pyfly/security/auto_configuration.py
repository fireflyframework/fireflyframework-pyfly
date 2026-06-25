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
    from pyfly.security.password import BcryptPasswordEncoder, DelegatingPasswordEncoder
except ImportError:
    BcryptPasswordEncoder = object  # type: ignore[misc,assignment]
    DelegatingPasswordEncoder = object  # type: ignore[misc,assignment]

try:
    from pyfly.security.oauth2.resource_server import (
        ClaimMappings,
        JWKSTokenValidator,
        discover_oidc,
    )
except ImportError:
    JWKSTokenValidator = object  # type: ignore[misc,assignment]
    ClaimMappings = object  # type: ignore[misc,assignment]
    discover_oidc = None  # type: ignore[assignment]

try:
    from pyfly.security.oauth2.properties import ResourceServerProperties
except ImportError:
    ResourceServerProperties = object  # type: ignore[misc,assignment]

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
from pyfly.kernel.exceptions import SecurityException

# The built-in placeholder secret shipped in defaults. Signing tokens with it
# would let anyone who knows the (public) framework default forge tokens, so the
# composition root refuses to start when it is left in place.
_PLACEHOLDER_SECRET = "change-me-in-production"
# Minimum HMAC key length for the HS family (RFC 7518 §3.2: a key of the same
# size as the hash output — 256 bits / 32 bytes — for HS256).
_MIN_HS_SECRET_BYTES = 32


def _resolve_signing_secret(config: Config, key: str, algorithm: str) -> str:
    """Read a token-signing secret from *key* and refuse insecure values.

    Raises:
        SecurityException: if the secret is unset (the built-in placeholder) or,
            for an HMAC (``HS*``) algorithm, shorter than 32 bytes.
    """
    secret = str(config.get(key, _PLACEHOLDER_SECRET))
    if secret == _PLACEHOLDER_SECRET:
        raise SecurityException(
            f"Refusing to start: '{key}' is unset, so the built-in placeholder secret "
            f"would be used to sign tokens. Set '{key}' to a strong, randomly-generated "
            f'value (e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`).',
            code="INSECURE_SIGNING_SECRET",
        )
    if algorithm.upper().startswith("HS") and len(secret.encode("utf-8")) < _MIN_HS_SECRET_BYTES:
        raise SecurityException(
            f"Refusing to start: '{key}' must be at least {_MIN_HS_SECRET_BYTES} bytes for "
            f"{algorithm} (RFC 7518 §3.2); got {len(secret.encode('utf-8'))} bytes.",
            code="WEAK_SIGNING_SECRET",
        )
    return secret


def _audience(config: Config, key: str) -> str | list[str] | None:
    """Read a comma-separated / list audience value (single value collapsed to a
    string), or ``None`` when unset."""
    raw = config.get(key)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        values = [str(a).strip() for a in raw if str(a).strip()]
    else:
        values = [a.strip() for a in str(raw).split(",") if a.strip()]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _as_bool(value: Any) -> bool:
    """Coerce a config value (bool or string like ``"true"``/``"false"``) to bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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
        algorithm = str(config.get("pyfly.security.jwt.algorithm", "HS256"))
        # The symmetric secret is only enforced when the symmetric JWT filter is
        # actually serving requests. A resource-server-only app (the recommended
        # setup) enables ``pyfly.security.enabled`` for the JWKS validator and never
        # uses this signer, so it must not be forced to invent a symmetric secret.
        filter_enabled = str(config.get("pyfly.security.jwt.filter.enabled", "false")).lower() == "true"
        if filter_enabled:
            secret = _resolve_signing_secret(config, "pyfly.security.jwt.secret", algorithm)
        else:
            secret = str(config.get("pyfly.security.jwt.secret", _PLACEHOLDER_SECRET))
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

    @bean
    @conditional_on_property("pyfly.security.password.delegating.enabled", having_value="true")
    def delegating_password_encoder(self, config: Config) -> DelegatingPasswordEncoder:
        # Opt-in Spring-style {id}-prefixed encoder (bcrypt default, recognises
        # {pbkdf2}/{scrypt}/{argon2}) enabling on-login algorithm migration.
        from pyfly.security.password import create_delegating_password_encoder

        rounds = int(config.get("pyfly.security.password.bcrypt-rounds", 12))
        return create_delegating_password_encoder(bcrypt_rounds=rounds)


# ---------------------------------------------------------------------------
# HTTP Basic authentication
# ---------------------------------------------------------------------------


def _csv_or_list(value: Any) -> list[str]:
    """Parse a comma-separated string or a list into a trimmed string list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


@auto_configuration
@conditional_on_property("pyfly.security.http-basic.enabled", having_value="true")
@conditional_on_class("starlette")
@conditional_on_class("bcrypt")
class HttpBasicAutoConfiguration:
    """Auto-configures HTTP Basic authentication from config (opt-in).

    Users are declared (with **pre-hashed** bcrypt passwords) under
    ``pyfly.security.http-basic.users``::

        pyfly:
          security:
            http-basic:
              enabled: true
              realm: "PyFly"
              error-mode: "401"        # or "anonymous"
              users:
                alice:
                  password-hash: "$2b$12$..."   # never plaintext
                  roles: "ADMIN,USER"

    Apps needing a dynamic user store register their own
    :class:`HttpBasicAuthenticationFilter` (a ``WebFilter`` bean) instead.
    """

    @bean
    def http_basic_filter(self, config: Config) -> WebFilter:
        from pyfly.security.password import BcryptPasswordEncoder
        from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails
        from pyfly.web.adapters.starlette.filters.http_basic_filter import HttpBasicAuthenticationFilter

        raw_users = config.get("pyfly.security.http-basic.users", {})
        users: list[UserDetails] = []
        if isinstance(raw_users, dict):
            for username, props in raw_users.items():
                if not isinstance(props, dict):
                    continue
                users.append(
                    UserDetails(
                        username=str(username),
                        password_hash=str(props.get("password-hash", "")),
                        roles=_csv_or_list(props.get("roles")),
                        permissions=_csv_or_list(props.get("permissions")),
                        enabled=_as_bool(props.get("enabled", True)),
                    )
                )

        rounds = int(config.get("pyfly.security.password.bcrypt-rounds", 12))
        return HttpBasicAuthenticationFilter(
            InMemoryUserDetailsService(*users),
            BcryptPasswordEncoder(rounds=rounds),
            realm=str(config.get("pyfly.security.http-basic.realm", "Realm")),
            error_mode=str(config.get("pyfly.security.http-basic.error-mode", "anonymous")),
        )


# ---------------------------------------------------------------------------
# OAuth2 Resource Server
# ---------------------------------------------------------------------------


@auto_configuration
@conditional_on_property("pyfly.security.oauth2.resource-server.enabled", having_value="true")
@conditional_on_class("jwt")
class OAuth2ResourceServerAutoConfiguration:
    """Auto-configures a multi-IdP :class:`JWKSTokenValidator`.

    Activated when ``pyfly.security.oauth2.resource-server.enabled=true`` and
    ``pyjwt`` is installed. Binds :class:`ResourceServerProperties` and works out
    of the box with Keycloak, Microsoft Entra ID (v1.0 + v2.0) and AWS Cognito —
    deriving the JWKS endpoint via OIDC discovery from ``issuer-uri`` when no
    explicit ``jwks-uri`` is given, and mapping authorities / scopes / principal
    from a configurable set of claim paths.
    """

    @bean
    @conditional_on_missing_bean(JWKSTokenValidator)
    def jwks_token_validator(self, config: Config) -> JWKSTokenValidator:
        props = config.bind(ResourceServerProperties)

        jwks_uri = props.jwks_uri
        issuer = props.issuer or None
        # OIDC discovery (Spring's ``issuer-uri``): derive the JWKS endpoint and
        # the authoritative issuer from the provider's discovery document.
        if not jwks_uri and props.issuer_uri:
            jwks_uri, discovered_issuer = discover_oidc(props.issuer_uri, timeout=float(props.jwks_timeout_seconds))
            issuer = issuer or discovered_issuer

        mappings = ClaimMappings(
            principal_claims=tuple(props.principal_claim_list()),
            authority_claims=tuple(props.authorities_claim_list()),
            scope_claims=tuple(props.scope_claim_list()),
            authority_prefix=props.authority_prefix,
            attribute_claims=tuple(props.attribute_claim_list()),
        )

        return JWKSTokenValidator(
            jwks_uri=jwks_uri,
            issuer=issuer,
            audiences=props.audience_list(),
            algorithms=props.algorithm_list(),
            leeway=props.clock_skew_seconds,
            validate_audience=props.validate_audience,
            claim_mappings=mappings,
            jwks_timeout=float(props.jwks_timeout_seconds),
            jwks_cache_seconds=props.jwks_cache_seconds,
        )

    @bean
    @conditional_on_class("starlette")
    def oauth2_resource_server_filter(self, token_validator: JWKSTokenValidator, config: Config) -> WebFilter:
        # Bearer-token resource-server filter; registered so the post-start
        # rescan adds it to the chain whenever the resource server is on (#41).
        from pyfly.web.adapters.starlette.filters.oauth2_resource_filter import OAuth2ResourceServerFilter

        props = config.bind(ResourceServerProperties)
        return OAuth2ResourceServerFilter(
            token_validator=token_validator,
            exclude_patterns=props.exclude_pattern_list(),
            error_mode=props.authenticate_error_mode,
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
        secret = _resolve_signing_secret(config, "pyfly.security.oauth2.authorization-server.secret", "HS256")
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
            audience=_audience(config, "pyfly.security.oauth2.authorization-server.audience"),
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
                        # PKCE on by default (RFC 9700 / OAuth 2.1); opt out per
                        # registration with ``use-pkce: false``.
                        use_pkce=_as_bool(props.get("use-pkce", True)),
                        # RFC 9207 iss enforcement (opt-in; iss is validated when
                        # present regardless).
                        require_iss=_as_bool(props.get("require-iss", False)),
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
