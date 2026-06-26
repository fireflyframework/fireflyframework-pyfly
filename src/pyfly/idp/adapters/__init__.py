# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Concrete IDP adapters."""

from __future__ import annotations

from pyfly.kernel.exceptions import SecurityException


def _require_password_grant_optin(allowed: bool, provider: str) -> None:
    """Refuse the Resource Owner Password Credentials (ROPC) grant unless opted in.

    The ``grant_type=password`` flow (forwarding raw user credentials to an external
    IdP) is removed by OAuth 2.1 and discouraged by RFC 9700 §2.4 — it cannot carry
    MFA/step-up, defeats federation, and trains users to enter credentials into the
    client. It is disabled by default; enable per-adapter with
    ``allow_password_grant=True`` (config: ``pyfly.idp.allow-password-grant=true``)
    only for a legacy integration with no migration path. Prefer the
    authorization_code + PKCE login flow instead.
    """
    if not allowed:
        raise SecurityException(
            f"The '{provider}' resource-owner-password (ROPC) login flow is disabled. "
            "It is removed by OAuth 2.1 / discouraged by RFC 9700 §2.4. Use the "
            "authorization_code + PKCE flow, or, only for a legacy integration, set "
            "'pyfly.idp.allow-password-grant=true' (or allow_password_grant=True).",
            code="ROPC_DISABLED",
        )
