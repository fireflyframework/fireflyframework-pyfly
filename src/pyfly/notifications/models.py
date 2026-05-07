# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""DTOs for the notifications module."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class EmailStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class EmailMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    sender: str = ""
    subject: str = ""
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    template_id: str | None = None
    template_data: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SmsMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    to: str = ""
    body: str = ""
    sender: str | None = None


@dataclass
class PushMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    device_tokens: list[str] = field(default_factory=list)
    title: str = ""
    body: str = ""
    data: dict[str, object] = field(default_factory=dict)


@dataclass
class NotificationResult:
    id: str
    provider: str
    status: EmailStatus
    provider_id: str | None = None
    error: str | None = None
