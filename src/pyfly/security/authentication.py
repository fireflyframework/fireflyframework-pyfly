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
"""Authentication SPI — Spring's ``AuthenticationManager`` / ``AuthenticationProvider``.

A :class:`ProviderManager` delegates an :class:`Authentication` request to the
first :class:`AuthenticationProvider` that ``supports`` it. The built-in
:class:`DaoAuthenticationProvider` checks a username/password against a
:class:`~pyfly.security.user_details.UserDetailsService` and a
:class:`~pyfly.security.password.PasswordEncoder`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext
from pyfly.security.password import PasswordEncoder
from pyfly.security.user_details import UserDetailsService


class AuthenticationException(SecurityException):
    """Base class for authentication failures."""


class BadCredentialsException(AuthenticationException):
    """The supplied credentials were invalid (or the principal is unknown)."""

    def __init__(self, message: str = "Bad credentials") -> None:
        super().__init__(message, code="BAD_CREDENTIALS")


class DisabledException(AuthenticationException):
    """The account exists but is disabled."""

    def __init__(self, message: str = "Account is disabled") -> None:
        super().__init__(message, code="ACCOUNT_DISABLED")


class ProviderNotFoundException(AuthenticationException):
    """No configured provider could handle the authentication request."""

    def __init__(self, message: str = "No authentication provider for this request") -> None:
        super().__init__(message, code="PROVIDER_NOT_FOUND")


@dataclass
class Authentication:
    """An authentication request or result (cf. Spring's ``Authentication``).

    Before authentication: ``principal`` + ``credentials`` are the submitted
    username/password. After: ``authenticated`` is True, ``authorities`` /
    ``roles`` / ``permissions`` are populated and ``credentials`` is erased.
    """

    principal: str
    credentials: str | None = None
    authorities: list[str] = field(default_factory=list)
    authenticated: bool = False
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_security_context(self) -> SecurityContext:
        """Build a :class:`SecurityContext` from this (authenticated) result."""
        return SecurityContext(
            user_id=self.principal if self.authenticated else None,
            roles=list(self.roles),
            permissions=list(self.permissions),
        )


@runtime_checkable
class AuthenticationProvider(Protocol):
    """Authenticates an :class:`Authentication` it ``supports``."""

    def supports(self, authentication: Authentication) -> bool: ...

    async def authenticate(self, authentication: Authentication) -> Authentication: ...


class DaoAuthenticationProvider:
    """Authenticates username/password against a UserDetailsService + PasswordEncoder."""

    def __init__(self, user_details_service: UserDetailsService, password_encoder: PasswordEncoder) -> None:
        self._users = user_details_service
        self._encoder = password_encoder
        # A throw-away hash so an unknown user still incurs a verify() — equalising
        # timing so the endpoint can't be used to enumerate valid usernames.
        self._dummy_hash = password_encoder.hash("pyfly-dummy-password")

    def supports(self, authentication: Authentication) -> bool:
        return bool(authentication.principal) and authentication.credentials is not None

    async def authenticate(self, authentication: Authentication) -> Authentication:
        user = await self._users.load_user_by_username(authentication.principal)
        credentials = authentication.credentials or ""
        if user is None:
            self._encoder.verify(credentials, self._dummy_hash)  # constant-time-ish
            raise BadCredentialsException()
        if not self._encoder.verify(credentials, user.password_hash):
            raise BadCredentialsException()
        if not user.enabled:
            raise DisabledException()
        return Authentication(
            principal=user.username,
            credentials=None,
            authenticated=True,
            roles=list(user.roles),
            permissions=list(user.permissions),
            authorities=[*user.roles, *user.permissions],
            details=dict(authentication.details),
        )


class ProviderManager:
    """An :class:`AuthenticationManager` that consults providers in order."""

    def __init__(self, *providers: AuthenticationProvider) -> None:
        self._providers: list[AuthenticationProvider] = list(providers)

    @classmethod
    def of(cls, providers: Iterable[AuthenticationProvider]) -> ProviderManager:
        return cls(*providers)

    async def authenticate(self, authentication: Authentication) -> Authentication:
        last_error: AuthenticationException | None = None
        supported = False
        for provider in self._providers:
            if not provider.supports(authentication):
                continue
            supported = True
            try:
                result = await provider.authenticate(authentication)
            except AuthenticationException as exc:
                last_error = exc
                continue
            if result.authenticated:
                result.credentials = None  # erase credentials on success
                return result
        if last_error is not None:
            raise last_error
        if not supported:
            raise ProviderNotFoundException()
        raise BadCredentialsException()
