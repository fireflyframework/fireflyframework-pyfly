# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""No-op e-signature adapter — useful for dev / tests."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    SignatureRequest,
)


class NoOpESignatureAdapter:
    """Marks every envelope as signed immediately — perfect for tests."""

    name = "noop"

    def __init__(self) -> None:
        self._envelopes: dict[str, ESignatureEnvelope] = {}
        self._lock = asyncio.Lock()

    async def send(self, request: SignatureRequest) -> ESignatureEnvelope:
        envelope = ESignatureEnvelope(
            provider=self.name,
            document_id=request.document_id,
            status=ESignatureStatus.SIGNED,
            provider_envelope_id=str(uuid.uuid4()),
            sent_at=datetime.now(UTC),
            signed_at=datetime.now(UTC),
        )
        async with self._lock:
            self._envelopes[envelope.id] = envelope
        return envelope

    async def get(self, envelope_id: str) -> ESignatureEnvelope | None:
        async with self._lock:
            return self._envelopes.get(envelope_id)

    async def cancel(self, envelope_id: str) -> bool:
        async with self._lock:
            envelope = self._envelopes.get(envelope_id)
            if envelope is None:
                return False
            envelope.status = ESignatureStatus.DECLINED
        return True
