# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Frozen, bounded archive facts and exact deterministic storage/signing bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from pyfly.audit.models import (
    AuditCheckpoint,
    AuditHead,
    AuditScope,
    CheckpointSignature,
    Digest,
    Fact,
    Identifier,
    JournalEntry,
    Revision,
    canonical_bytes,
    chain_id,
    entry_digest,
    genesis_hash,
)

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
ObjectRef = Annotated[str, Field(strict=True, min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_.:/+=-]+$")]
ByteCount = Annotated[int, Field(strict=True, ge=0, le=MAX_ARCHIVE_BYTES)]


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def byte_limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_ARCHIVE_BYTES:
        raise ValueError("Archive byte limit is outside the supported bounds")


class ArchiveCapabilities(Fact):
    sink_id: Identifier
    backend: Identifier
    conditional_create: Annotated[bool, Field(strict=True)]
    durable_ack: Annotated[bool, Field(strict=True)]
    versioned: Annotated[bool, Field(strict=True)]
    retention_lock: Annotated[bool, Field(strict=True)]
    independent_custody: Annotated[bool, Field(strict=True)]


class ArchiveReceipt(Fact):
    sink_id: Identifier
    content_id: Digest
    byte_count: ByteCount
    object_ref: ObjectRef
    version_ref: ObjectRef | None = None
    retention_status: Literal["unlocked", "locked", "unknown"]
    retained_until: AwareDatetime | None = None
    legal_hold: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def reference_and_retention(self) -> Self:
        if any("://" in value for value in (self.object_ref, self.version_ref) if value is not None):
            raise ValueError("Archive references must be opaque identifiers, never URLs")
        if self.retention_status == "unlocked" and (self.retained_until is not None or self.legal_hold is True):
            raise ValueError("Unlocked archive receipts cannot claim retention protection")
        return self


class ArchiveManifest(Fact):
    schema_version: Literal[1] = 1
    journal_id: Identifier
    scope: AuditScope
    chain_id: Digest
    first_sequence: Revision
    last_sequence: Revision
    entry_count: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_ENTRIES)]
    previous_hash: Digest
    terminal_hash: Digest
    payload_digest: Digest
    payload_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)]
    canonicalization: Literal["pyfly-json-v1"] = "pyfly-json-v1"

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Archive schema version must be an integer")
        return value

    @model_validator(mode="after")
    def identities_and_range(self) -> Self:
        if self.chain_id != chain_id(self.journal_id, self.scope):
            raise ValueError("Archive manifest scope does not match its chain")
        if self.last_sequence - self.first_sequence + 1 != self.entry_count:
            raise ValueError("Archive manifest must describe an exact contiguous range")
        if self.first_sequence == 1 and self.previous_hash != genesis_hash(self.chain_id):
            raise ValueError("Archive first entry does not follow its deterministic genesis")
        return self

    @property
    def delivery_id(self) -> str:
        return content_digest(
            b"pyfly-audit-delivery-v1\x00"
            + canonical_bytes(
                [
                    self.journal_id,
                    self.chain_id,
                    self.first_sequence,
                    self.last_sequence,
                    self.terminal_hash,
                ]
            )
        )


class ArchiveSegment(Fact):
    manifest: ArchiveManifest
    payload: Annotated[bytes, Field(strict=True, repr=False, min_length=1, max_length=MAX_ARCHIVE_BYTES)]

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if (
            len(self.payload) != self.manifest.payload_bytes
            or content_digest(self.payload) != self.manifest.payload_digest
        ):
            raise ValueError("Archive payload digest or size does not match its manifest")
        self.entries()
        if len(self.to_bytes()) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive segment exceeds its hard byte bound")
        return self

    @classmethod
    def from_entries(cls, entries: Sequence[JournalEntry], *, max_bytes: int = MAX_ARCHIVE_BYTES) -> ArchiveSegment:
        byte_limit(max_bytes)
        if not isinstance(entries, (tuple, list)) or not 1 <= len(entries) <= MAX_ARCHIVE_ENTRIES:
            raise ValueError("Archive segments require a bounded nonempty entry sequence")
        validated = tuple(JournalEntry.model_validate(entry) for entry in entries)
        chunks, total = [], 0
        for entry in validated:
            chunk = entry.to_bytes() + b"\n"
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Archive segment exceeds its configured byte limit")
            chunks.append(chunk)
        payload = b"".join(chunks)
        first, last = validated[0], validated[-1]
        result = cls(
            manifest=ArchiveManifest(
                journal_id=first.journal_id,
                scope=first.event.scope,
                chain_id=first.chain_id,
                first_sequence=first.sequence,
                last_sequence=last.sequence,
                entry_count=len(validated),
                previous_hash=first.previous_hash,
                terminal_hash=last.entry_hash,
                payload_digest=content_digest(payload),
                payload_bytes=len(payload),
            ),
            payload=payload,
        )
        if len(result.to_bytes()) > max_bytes:
            raise ValueError("Archive segment exceeds its configured byte limit")
        return result

    def entries(self) -> tuple[JournalEntry, ...]:
        if not self.payload.endswith(b"\n"):
            raise ValueError("Archive NDJSON is incomplete")
        count = self.payload.count(b"\n")
        if not 1 <= count <= MAX_ARCHIVE_ENTRIES or count != self.manifest.entry_count:
            raise ValueError("Archive NDJSON count does not match its declared range")
        lines = self.payload[:-1].split(b"\n")
        records = []
        previous = self.manifest.previous_hash
        for sequence, line in enumerate(lines, self.manifest.first_sequence):
            entry = JournalEntry.model_validate_json(line)
            if (
                entry.to_bytes() != line
                or entry_digest(entry) != entry.entry_hash
                or entry.sequence != sequence
                or entry.previous_hash != previous
                or entry.chain_id != self.manifest.chain_id
                or entry.journal_id != self.manifest.journal_id
                or entry.event.scope != self.manifest.scope
            ):
                raise ValueError("Archive entry identity, canonical bytes or continuity is invalid")
            records.append(entry)
            previous = entry.entry_hash
        if previous != self.manifest.terminal_hash:
            raise ValueError("Archive terminal hash does not match its manifest")
        return tuple(records)

    def to_bytes(self) -> bytes:
        return (
            b"pyfly-audit-segment-v1\n" + canonical_bytes(self.manifest.model_dump(mode="json")) + b"\n" + self.payload
        )

    @property
    def content_id(self) -> str:
        return content_digest(self.to_bytes())

    @classmethod
    def from_bytes(cls, content: bytes, *, max_bytes: int = MAX_ARCHIVE_BYTES) -> ArchiveSegment:
        byte_limit(max_bytes)
        if (
            type(content) is not bytes
            or len(content) > max_bytes
            or not content.startswith(b"pyfly-audit-segment-v1\n")
        ):
            raise ValueError("Archive segment has an unsupported frame or size")
        manifest_bytes, separator, payload = content.removeprefix(b"pyfly-audit-segment-v1\n").partition(b"\n")
        if not separator:
            raise ValueError("Archive segment frame is incomplete")
        manifest = ArchiveManifest.model_validate_json(manifest_bytes)
        if canonical_bytes(manifest.model_dump(mode="json")) != manifest_bytes:
            raise ValueError("Archive manifest is not canonical")
        return cls(manifest=manifest, payload=payload)


class ArchiveCheckpoint(Fact):
    audit: AuditCheckpoint
    manifest: ArchiveManifest
    segment_receipt: ArchiveReceipt

    @model_validator(mode="after")
    def bind_manifest(self) -> Self:
        if (
            self.audit.journal_id != self.manifest.journal_id
            or self.audit.scope != self.manifest.scope
            or self.audit.head
            != AuditHead(
                chain_id=self.manifest.chain_id,
                sequence=self.manifest.last_sequence,
                entry_hash=self.manifest.terminal_hash,
            )
        ):
            raise ValueError("Archive checkpoint does not bind the exact manifest boundary")
        return self

    def to_bytes(self) -> bytes:
        return b"pyfly-audit-archive-checkpoint-v1\x00" + canonical_bytes(self.model_dump(mode="json"))


class SignedArchiveCheckpoint(Fact):
    checkpoint: ArchiveCheckpoint
    signature: CheckpointSignature

    def to_bytes(self) -> bytes:
        return b"pyfly-audit-signed-archive-v1\n" + canonical_bytes(self.model_dump(mode="json"))

    @classmethod
    def from_bytes(cls, content: bytes) -> SignedArchiveCheckpoint:
        if (
            type(content) is not bytes
            or len(content) > 32768
            or not content.startswith(b"pyfly-audit-signed-archive-v1\n")
        ):
            raise ValueError("Signed archive checkpoint has an unsupported frame or size")
        value = cls.model_validate_json(content.removeprefix(b"pyfly-audit-signed-archive-v1\n"))
        if value.to_bytes() != content:
            raise ValueError("Signed archive checkpoint is not canonical")
        return value


class ArchiveDeliveryReceipt(Fact):
    delivery_id: Digest
    segment: ArchiveReceipt
    checkpoint: ArchiveReceipt


class ArchiveVerification(Fact):
    checkpoint: ArchiveCheckpoint
    entries: tuple[JournalEntry, ...] = Field(min_length=1, max_length=MAX_ARCHIVE_ENTRIES)
    signature_verified: Literal[True] = True
    trusted_head_matched: Annotated[bool, Field(strict=True)] = False


class ArchiveChainVerification(Fact):
    entries: tuple[JournalEntry, ...] = Field(min_length=1, max_length=MAX_ARCHIVE_ENTRIES)
    segment_count: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_ENTRIES)]
    starts_at_genesis: Annotated[bool, Field(strict=True)]
    signature_verified: Literal[True] = True
    trusted_head_matched: Annotated[bool, Field(strict=True)] = False

    @property
    def head(self) -> AuditHead:
        return self.entries[-1].head


class AuditArchiveNotice(Fact):
    journal_id: Identifier
    scope: AuditScope
    chain_id: Digest
    sequence: Revision
    entry_hash: Digest

    @model_validator(mode="after")
    def bind_scope(self) -> Self:
        if self.chain_id != chain_id(self.journal_id, self.scope):
            raise ValueError("Archive notice scope does not identify its journal chain")
        return self
