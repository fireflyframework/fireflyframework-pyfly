# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Optional OAuth acquisition primitives, independent of web routes and IdP administration.

Install ``pyfly[oauth2-client]``. Endpoints are explicit trusted configuration;
callback validation, credential persistence and domain authorization belong to callers.
"""

from pyfly.oauth2.acquisition import (
    DeviceAuthorization,
    OAuth2Client,
    OAuth2ClientError,
    OAuth2Endpoints,
    OAuth2Tokens,
    PKCEPair,
    generate_pkce,
    pkce_challenge,
)

__all__ = [
    "DeviceAuthorization",
    "OAuth2Client",
    "OAuth2ClientError",
    "OAuth2Endpoints",
    "OAuth2Tokens",
    "PKCEPair",
    "generate_pkce",
    "pkce_challenge",
]
