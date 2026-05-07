# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Logalty (EU qualified e-signature) adapter — REST API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    SignatureRequest,
)


class LogaltyESignatureAdapter:
    """Bridge to Logalty's eIDAS-compliant signing service.

    Args:
        api_base: tenant-specific API root.
        api_key: API key.
    """

    name = "logalty"

    def __init__(self, *, api_base: str, api_key: str) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "LogaltyESignatureAdapter requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(timeout=60.0)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def send(self, request: SignatureRequest) -> ESignatureEnvelope:
        async with await self._client() as client:
            payload = {
                "documentId": request.document_id,
                "subject": request.subject,
                "message": request.message,
                "signers": [{"name": r.name, "email": r.email, "role": r.role} for r in request.recipients],
            }
            resp = await client.post(f"{self._api_base}/envelopes", json=payload, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
            return ESignatureEnvelope(
                provider=self.name,
                document_id=request.document_id,
                status=ESignatureStatus.SENT,
                provider_envelope_id=data.get("envelopeId"),
                sent_at=datetime.now(UTC),
            )

    async def get(self, envelope_id: str) -> ESignatureEnvelope | None:
        async with await self._client() as client:
            resp = await client.get(f"{self._api_base}/envelopes/{envelope_id}", headers=self._headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return ESignatureEnvelope(
                provider=self.name,
                document_id="",
                status=_map_status(data.get("status", "SENT")),
                provider_envelope_id=envelope_id,
            )

    async def cancel(self, envelope_id: str) -> bool:
        async with await self._client() as client:
            resp = await client.delete(f"{self._api_base}/envelopes/{envelope_id}", headers=self._headers)
            return resp.status_code in (200, 204)


def _map_status(value: str) -> ESignatureStatus:
    return {
        "DRAFT": ESignatureStatus.DRAFT,
        "SENT": ESignatureStatus.SENT,
        "PENDING": ESignatureStatus.SENT,
        "SIGNED": ESignatureStatus.SIGNED,
        "COMPLETED": ESignatureStatus.SIGNED,
        "DECLINED": ESignatureStatus.DECLINED,
        "EXPIRED": ESignatureStatus.EXPIRED,
    }.get(value.upper(), ESignatureStatus.SENT)
