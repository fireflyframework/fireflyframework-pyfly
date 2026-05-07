# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Models / DTOs for the callbacks module."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CallbackStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


@dataclass
class AuthorizedDomain:
    domain: str
    description: str = ""


@dataclass
class CallbackSubscription:
    event_type: str
    target_url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CallbackConfig:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = ""
    name: str = ""
    enabled: bool = True
    subscriptions: list[CallbackSubscription] = field(default_factory=list)
    authorized_domains: list[AuthorizedDomain] = field(default_factory=list)
    secret: str | None = None
    max_attempts: int = 5
    backoff_ms: int = 5_000


@dataclass
class CallbackExecution:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    config_id: str = ""
    event_type: str = ""
    target_url: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: CallbackStatus = CallbackStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    response_status: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
