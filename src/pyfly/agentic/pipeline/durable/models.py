# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Closed durable execution facts; checkpoint content never belongs in audit events."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import AwareDatetime, Field, JsonValue, ValidationError, field_validator, model_validator

from pyfly.agentic.exceptions import PipelinePersistenceError
from pyfly.agentic.pipeline.checkpoint import CheckpointRecord
from pyfly.audit.models import AuditActor, AuditScope, Count, Digest, Fact, Identifier, Revision


class DurableStateError(PipelinePersistenceError):
    """A content-free non-retryable durable state or storage failure."""


class DurableConflict(DurableStateError):
    """A stale generation, lease or conflicting one-shot transition."""


def bounded_json(value: object, *, max_bytes: int = 1024 * 1024) -> str:
    """Validate depth/types before serialization; return detached canonical JSON."""
    try:
        if type(max_bytes) is not int or not 32 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError
        pending = [(value, 0)]
        visited = 0
        while pending:
            item, depth = pending.pop()
            visited += 1
            if depth > 32 or visited > 100_000:
                raise ValueError
            if type(item) is dict:
                if not all(type(k) is str for k in item):
                    raise ValueError
                pending.extend((v, depth + 1) for v in item.values())
            elif type(item) in (list, tuple):
                pending.extend((v, depth + 1) for v in cast(list[object] | tuple[object, ...], item))
            elif (
                item is not None
                and type(item) not in (str, int, float, bool)
                or type(item) is float
                and not math.isfinite(item)
            ):
                raise ValueError
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
        if len(serialized.encode()) > max_bytes:
            raise ValueError
        return serialized
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise DurableStateError("Durable payload is not bounded JSON") from None


def checkpoint_json(record: CheckpointRecord, *, max_bytes: int = 1024 * 1024) -> str:
    """Validate writer fields before extracting plain JSON; never serialize a forged DTO."""
    try:
        if not isinstance(record, CheckpointRecord):
            raise ValueError
        validated = CheckpointRecord.model_validate(record)
        fields = {name: getattr(validated, name) for name in CheckpointRecord.model_fields}
        return bounded_json(fields, max_bytes=max_bytes)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise DurableStateError("Durable checkpoint reference is invalid") from None


class DurableRunKey(Fact):
    scope: AuditScope
    run_id: Identifier


class DurableBinding(Fact):
    pipeline_name: Identifier
    definition_id: Identifier
    definition_revision: Revision
    graph_version: Identifier
    graph_digest: Digest
    plan_digest: Digest
    node_ids: Annotated[tuple[Identifier, ...], Field(min_length=3, max_length=64)]
    pause_node_id: Identifier

    @model_validator(mode="after")
    def linear_boundary(self) -> Self:
        if len(set(self.node_ids)) != len(self.node_ids) or self.pause_node_id not in self.node_ids[1:-1]:
            raise ValueError("Durable binding requires unique nodes and an interior pause")
        return self


class DurableClaim(Fact):
    key: DurableRunKey
    binding: DurableBinding
    owner: Identifier
    generation: Revision
    epoch: Revision
    expires_at: AwareDatetime

    @field_validator("expires_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DurableApproval(Fact):
    task_id: Identifier
    node_id: Identifier
    generation: Revision
    decision: Literal["approve", "reject"] | None = None
    actor: AuditActor | None = None
    idempotency_key: Identifier | None = None
    policy_digest: Digest | None = None

    @model_validator(mode="after")
    def complete_decision(self) -> Self:
        fields = (self.actor, self.idempotency_key, self.policy_digest)
        if (self.decision is None and any(v is not None for v in fields)) or (
            self.decision is not None and any(v is None for v in fields)
        ):
            raise ValueError("Durable decision fields must be complete")
        return self


DurableStatus = Literal[
    "queued", "running", "waiting_approval", "rejected", "succeeded", "failed", "unknown", "cancelled"
]


class DurableRunEnvelope(Fact):
    schema_version: Literal[1] = 1
    key: DurableRunKey
    binding: DurableBinding
    original_input: JsonValue
    initiator: AuditActor
    status: DurableStatus = "queued"
    version: Revision = 1
    generation: Revision = 1
    epoch: Count = 0
    checkpoint: CheckpointRecord | None = None
    task: DurableApproval | None = None
    lease: DurableClaim | None = None
    last_claim: DurableClaim | None = None
    cancel_actor: AuditActor | None = None


DurableEventKind = Literal[
    "created",
    "claimed",
    "renewed",
    "checkpointed",
    "waiting_approval",
    "approved",
    "rejected",
    "succeeded",
    "failed",
    "unknown",
    "cancelled",
]


class DurableEvent(Fact):
    schema_version: Literal[1] = 1
    key: DurableRunKey
    sequence: Revision
    kind: DurableEventKind
    status: DurableStatus
    generation: Revision
    epoch: Count
    node_id: Identifier | None = None
    actor: AuditActor
    occurred_at: AwareDatetime
