# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Immutable v2 identities, observations and staging-transfer receipts."""

from dataclasses import dataclass, field
from typing import Literal

Provider = Literal["sharepoint", "onedrive", "google_drive"]


def bounded(value: str, *, limit: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Expected a bounded nonempty string without controls")
    return value


@dataclass(frozen=True, slots=True)
class Account:
    """Verified delegated subject, never an email address or application identity."""

    provider: Provider
    subject: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"sharepoint", "onedrive", "google_drive"}:
            raise ValueError("Unsupported source provider")
        bounded(self.subject)
        if self.tenant_id is not None:
            bounded(self.tenant_id)


@dataclass(frozen=True, slots=True)
class SourceScope:
    account: Account
    drive_id: str

    def __post_init__(self) -> None:
        bounded(self.drive_id)


@dataclass(frozen=True, slots=True)
class Cursor:
    scope: SourceScope
    operation: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        bounded(self.operation, limit=2048)
        bounded(self.token, limit=16384)


@dataclass(frozen=True, slots=True)
class RemoteItem:
    scope: SourceScope
    item_id: str
    name: str
    kind: Literal["file", "folder", "shortcut"]
    version: str | None
    size_bytes: int | None
    mime_type: str
    parents: tuple[str, ...]
    can_download: bool | None = None
    modified_at: str | None = None


@dataclass(frozen=True, slots=True)
class Change:
    kind: Literal["upsert", "deleted", "removed"]
    item_id: str
    item: RemoteItem | None = None


@dataclass(frozen=True, slots=True)
class ChangePage:
    changes: tuple[Change, ...]
    next_cursor: Cursor | None
    checkpoint: Cursor | None


@dataclass(frozen=True, slots=True)
class ItemPage:
    items: tuple[RemoteItem, ...]
    next_cursor: Cursor | None


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    scope: SourceScope
    item_id: str
    version: str
    size_bytes: int
    sha256: str
    mime_type: str
