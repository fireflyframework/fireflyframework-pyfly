# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Trusted application extensions around the framework-owned authorization-code flow."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

    from pyfly.security.context import SecurityContext
    from pyfly.security.oauth2.client import ClientRegistration
    from pyfly.security.oauth2.resource_server import JWKSTokenValidator


class OAuth2AuthorizationRequestCustomizer(ABC):
    """Return extra provider parameters; native state, nonce, PKCE and client fields are reserved."""

    @abstractmethod
    async def customize(self, request: Request, registration: ClientRegistration) -> Mapping[str, str]: ...


class OAuth2AuthenticationSuccessHandler(ABC):
    """Authorize the verified principal before any new identity is published to the session."""

    @abstractmethod
    async def on_success(
        self, request: Request, registration: ClientRegistration, context: SecurityContext
    ) -> None: ...


class OidcTokenValidatorFactory(ABC):
    """Supply a native signature validator; mandatory OIDC claim checks remain in the login flow."""

    @abstractmethod
    def create(self, registration: ClientRegistration) -> JWKSTokenValidator: ...
