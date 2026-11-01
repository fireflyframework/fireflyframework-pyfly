# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Audit contracts independent of SQL adapters and key custody."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pyfly.audit.models import AuditEvent, AuditHead, AuditScope, CheckpointSignature, JournalEntry, VerificationResult


class AuditError(RuntimeError):
    """Safe error messages contain no input facts, SQL or credential material."""


class AuditConflict(AuditError):
    pass


class AuditIntegrityError(AuditError):
    pass


class AuditAdmissionRefused(AuditError):
    """An existing intent permits no further automatic external effect."""

    retryable = False


@runtime_checkable
class AuditAdmissionJournal(Protocol):
    """Optional atomic create-only intent capability; identical existing facts are refused."""

    async def admit_once(self, event: AuditEvent) -> JournalEntry: ...


@runtime_checkable
class AuditJournal(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def append(self, event: AuditEvent) -> JournalEntry: ...
    async def head(self, scope: AuditScope) -> AuditHead: ...
    async def read(
        self, scope: AuditScope, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[JournalEntry, ...]: ...
    async def verify(self, scope: AuditScope, *, trusted_head: AuditHead | None = None) -> VerificationResult: ...
    async def export(
        self, scope: AuditScope, *, after_sequence: int = 0, through_sequence: int, limit: int = 100
    ) -> bytes: ...


@runtime_checkable
class CheckpointSigner(Protocol):
    async def sign(self, checkpoint_bytes: bytes) -> CheckpointSignature: ...


@runtime_checkable
class CheckpointVerifier(Protocol):
    async def verify(self, checkpoint_bytes: bytes, signature: CheckpointSignature) -> bool: ...
