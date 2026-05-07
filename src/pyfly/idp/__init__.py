# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly identity-provider (IDP) abstraction.

Mirrors ``org.fireflyframework.idp``: pluggable user management,
authentication, MFA, role / scope management, session introspection.
Concrete adapters (Keycloak, Cognito, Azure AD, internal-DB) live in
``pyfly.idp.adapters``.
"""

from __future__ import annotations

from pyfly.idp.adapters.aws_cognito import AwsCognitoIdpAdapter
from pyfly.idp.adapters.azure_ad import AzureAdIdpAdapter
from pyfly.idp.adapters.internal_db import InternalDbIdpAdapter
from pyfly.idp.adapters.keycloak import KeycloakIdpAdapter
from pyfly.idp.models import (
    AuthResult,
    IdpRole,
    IdpUser,
    LoginRequest,
    MfaChallenge,
    PasswordChangeRequest,
    SessionIntrospection,
)
from pyfly.idp.port import IdpAdapter

__all__ = [
    "AuthResult",
    "AwsCognitoIdpAdapter",
    "AzureAdIdpAdapter",
    "IdpAdapter",
    "IdpRole",
    "IdpUser",
    "InternalDbIdpAdapter",
    "KeycloakIdpAdapter",
    "LoginRequest",
    "MfaChallenge",
    "PasswordChangeRequest",
    "SessionIntrospection",
]
