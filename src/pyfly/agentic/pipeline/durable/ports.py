# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Async native run state operations; mutations join the caller's primary Data unit."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pyfly.agentic.pipeline.checkpoint import CheckpointRecord
from pyfly.agentic.pipeline.durable.models import (
    DurableBinding,
    DurableClaim,
    DurableEvent,
    DurableRunEnvelope,
    DurableRunKey,
)
from pyfly.audit.models import AuditActor


@runtime_checkable
class DurableRunStore(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create(
        self, key: DurableRunKey, binding: DurableBinding, original_input: object, *, actor: AuditActor
    ) -> DurableRunEnvelope: ...
    async def load(self, key: DurableRunKey, binding: DurableBinding) -> DurableRunEnvelope: ...
    async def claim(
        self,
        key: DurableRunKey,
        binding: DurableBinding,
        *,
        owner: str,
        expected_generation: int,
        ttl_seconds: int = 30,
    ) -> DurableClaim: ...
    async def renew(self, claim: DurableClaim, *, ttl_seconds: int = 30) -> DurableClaim: ...
    async def validate_claim(self, claim: DurableClaim, *, node_id: str | None = None) -> DurableRunEnvelope: ...
    async def checkpoint(self, claim: DurableClaim, record: CheckpointRecord) -> DurableRunEnvelope: ...
    async def checkpoint_for(self, claim: DurableClaim) -> CheckpointRecord | None: ...
    async def decide(
        self,
        key: DurableRunKey,
        binding: DurableBinding,
        *,
        task_id: str,
        expected_generation: int,
        decision: Literal["approve", "reject"],
        actor: AuditActor,
        idempotency_key: str,
        policy_digest: str,
    ) -> DurableRunEnvelope: ...
    async def finish(
        self, claim: DurableClaim, *, outcome: Literal["succeeded", "failed", "unknown"]
    ) -> DurableRunEnvelope: ...
    async def cancel(
        self, key: DurableRunKey, binding: DurableBinding, *, actor: AuditActor, expected_version: int
    ) -> DurableRunEnvelope: ...
    async def events(
        self, key: DurableRunKey, binding: DurableBinding, *, after: int = 0, limit: int = 100
    ) -> tuple[DurableEvent, ...]: ...
