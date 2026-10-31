# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Bounded immutable reference facts. Content, credentials and arbitrary detail are not audit fields."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
CANONICALIZATION: Literal["pyfly-json-v1"] = "pyfly-json-v1"


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, revalidate_instances="always")


class AuditScope(Fact):
    tenant_id: Identifier
    workspace_id: Identifier
    project_id: Identifier


class AuditActor(Fact):
    kind: Literal["human", "agent", "service", "system"]
    subject_id: Identifier
    issuer_ref: Identifier | None = None
    initiating_subject_id: Identifier | None = None
    delegation_ref: Identifier | None = None


class AuditResource(Fact):
    kind: Identifier
    resource_id: Identifier
    revision: Revision | None = None
    digest: Digest | None = None


class AuditCorrelation(Fact):
    operation_id: Identifier | None = None
    request_id: Identifier | None = None
    causation_event_id: Identifier | None = None
    run_id: Identifier | None = None
    rollout_id: Identifier | None = None
    node_id: Identifier | None = None
    worker_id: Identifier | None = None
    receipt_id: Identifier | None = None
    claim_epoch: Count | None = None
    attempt: Count | None = None
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    span_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")] | None = None


class AuditUsage(Fact):
    status: Literal["reserved", "measured", "unknown", "not_reported"]
    requests: Count | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    spent_microusd: Count | None = None
    reserved_microusd: Count | None = None
    rate_ref: Identifier | None = None
    rate_revision: Revision | None = None


class AuditEvent(Fact):
    event_id: Identifier
    event_type: Identifier
    event_version: Revision = 1
    scope: AuditScope
    actor: AuditActor
    resource: AuditResource
    occurred_at: AwareDatetime
    outcome: Literal[
        "intent",
        "admitted",
        "denied",
        "approval_required",
        "started",
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
        "reconciled",
        "observed",
    ]
    correlation: AuditCorrelation = Field(default_factory=AuditCorrelation)
    references: tuple[AuditResource, ...] = Field(default=(), max_length=16)
    reason_codes: tuple[Identifier, ...] = Field(default=(), max_length=16)
    usage: AuditUsage | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class AuditHead(Fact):
    chain_id: Digest
    sequence: Count
    entry_hash: Digest


class JournalEntry(Fact):
    schema_version: Literal[1] = 1
    canonicalization: Literal["pyfly-json-v1"] = CANONICALIZATION
    journal_id: Identifier
    chain_id: Digest
    sequence: Revision
    previous_hash: Digest
    entry_hash: Digest
    recorded_at: AwareDatetime
    event: AuditEvent

    @property
    def head(self) -> AuditHead:
        return AuditHead(chain_id=self.chain_id, sequence=self.sequence, entry_hash=self.entry_hash)

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))


class VerificationResult(Fact):
    head: AuditHead
    anchored: bool


class CheckpointSignature(Fact):
    """Key references are identifiers, never key material or a URL. Custody belongs to the signer."""

    key_ref: Identifier
    algorithm: Identifier
    signature: Annotated[str, Field(pattern=r"^[A-Za-z0-9+/=_-]{1,4096}$", repr=False)]


class AuditCheckpoint(Fact):
    journal_id: Identifier
    scope: AuditScope
    head: AuditHead
    issued_at: AwareDatetime
    prior_checkpoint_hash: Digest | None = None

    @model_validator(mode="after")
    def bind_identity(self) -> Self:
        if self.head.chain_id != chain_id(self.journal_id, self.scope):
            raise ValueError("Audit checkpoint scope does not match its journal chain")
        return self

    def to_bytes(self) -> bytes:
        return b"pyfly-audit-checkpoint-v1\x00" + canonical_bytes(self.model_dump(mode="json"))


def canonical_bytes(value: object) -> bytes:
    """pyfly-json-v1: sorted Python JSON, ASCII escaping, integer-only numbers; explicitly not JCS."""

    def validate(item: object) -> None:
        if item is None or isinstance(item, (str, bool)):
            return
        if type(item) is int and -(2**63) <= item <= 2**63 - 1:
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("Audit canonicalization accepts bounded integer JSON facts only")

    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def chain_id(journal_id: str, scope: AuditScope) -> str:
    return hashlib.sha256(b"pyfly-audit-chain-v1\x00" + canonical_bytes([journal_id, scope.model_dump()])).hexdigest()


def genesis_hash(chain: str) -> str:
    return hashlib.sha256(b"pyfly-audit-genesis-v1\x00" + chain.encode("ascii")).hexdigest()


def entry_digest(entry: JournalEntry) -> str:
    return hashlib.sha256(
        b"pyfly-audit-entry-v1\x00" + canonical_bytes(entry.model_dump(mode="json", exclude={"entry_hash"}))
    ).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)
