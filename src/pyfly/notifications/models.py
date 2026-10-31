# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""DTOs for the notifications module."""

from __future__ import annotations

import re
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
    SUPPRESSED = "SUPPRESSED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Attachment:
    filename: str = field(repr=False)
    content_type: str = field(repr=False)
    data: bytes = field(repr=False)
    content_id: str | None = field(default=None, repr=False)
    inline: bool = False

    def __post_init__(self) -> None:
        if type(self.inline) is not bool or self.inline != (self.content_id is not None):
            raise ValueError("Inline attachments require an explicit content ID")
        if self.content_id is not None and (
            not isinstance(self.content_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,254}", self.content_id) is None
        ):
            raise ValueError("Attachment content ID must be a bounded canonical CID identifier")


@dataclass
class EmailMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    to: list[str] = field(default_factory=list, repr=False)
    cc: list[str] = field(default_factory=list, repr=False)
    bcc: list[str] = field(default_factory=list, repr=False)
    sender: str = field(default="", repr=False)
    subject: str = field(default="", repr=False)
    body_text: str | None = field(default=None, repr=False)
    body_html: str | None = field(default=None, repr=False)
    attachments: list[Attachment] = field(default_factory=list, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    template_id: str | None = field(default=None, repr=False)
    template_data: dict[str, object] = field(default_factory=dict, repr=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SmsMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    to: str = ""
    body: str = field(default="", repr=False)
    sender: str | None = None


@dataclass
class PushMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    device_tokens: list[str] = field(default_factory=list, repr=False)
    title: str = ""
    body: str = field(default="", repr=False)
    data: dict[str, object] = field(default_factory=dict, repr=False)


@dataclass
class NotificationResult:
    id: str
    provider: str
    status: EmailStatus
    provider_id: str | None = None
    error: str | None = field(default=None, repr=False)
