# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ECM DTOs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


@dataclass
class Folder:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    parent_id: str | None = None
    path: str = "/"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class DocumentVersion:
    version: int
    content_hash: str
    size_bytes: int
    storage_uri: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Document:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    folder_id: str | None = None
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    metadata: dict[str, object] = field(default_factory=dict)
    versions: list[DocumentVersion] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ESignatureStatus(StrEnum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    SIGNED = "SIGNED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


@dataclass
class Recipient:
    name: str
    email: str
    role: str = "signer"


@dataclass
class SignatureRequest:
    document_id: str
    recipients: list[Recipient]
    subject: str = "Please sign"
    message: str = ""


@dataclass
class ESignatureEnvelope:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider: str = ""
    document_id: str = ""
    status: ESignatureStatus = ESignatureStatus.DRAFT
    provider_envelope_id: str | None = None
    sent_at: datetime | None = None
    signed_at: datetime | None = None
