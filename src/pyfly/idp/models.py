# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""IDP DTOs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class IdpRole:
    name: str
    description: str = ""
    scopes: list[str] = field(default_factory=list)


@dataclass
class IdpUser:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: str = ""
    email: str = ""
    enabled: bool = True
    email_verified: bool = False
    first_name: str = ""
    last_name: str = ""
    roles: list[str] = field(default_factory=list)
    attributes: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LoginRequest:
    username: str
    password: str
    mfa_code: str | None = None


@dataclass
class PasswordChangeRequest:
    user_id: str
    old_password: str
    new_password: str


@dataclass
class MfaChallenge:
    challenge_id: str
    user_id: str
    method: str = "TOTP"


@dataclass
class AuthResult:
    user: IdpUser
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 3600
    token_type: str = "Bearer"
    mfa_required: bool = False
    mfa_challenge: MfaChallenge | None = None


@dataclass
class SessionIntrospection:
    active: bool
    user_id: str | None = None
    username: str | None = None
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
