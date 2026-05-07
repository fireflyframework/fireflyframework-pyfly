# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Adobe Sign / Adobe Acrobat Sign e-signature adapter — REST API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    SignatureRequest,
)


class AdobeSignESignatureAdapter:
    """Bridge to Adobe Sign's v6 REST API.

    Args:
        api_base: e.g. ``https://api.eu1.adobesign.com/api/rest/v6``.
        access_token: integration key or OAuth access token.
    """

    name = "adobe-sign"

    def __init__(self, *, api_base: str, access_token: str) -> None:
        self._api_base = api_base.rstrip("/")
        self._access_token = access_token

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # noqa: BLE001
            msg = "AdobeSignESignatureAdapter requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(timeout=60.0)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def send(self, request: SignatureRequest) -> ESignatureEnvelope:
        async with await self._client() as client:
            payload = {
                "fileInfos": [{"transientDocumentId": request.document_id}],
                "name": request.subject,
                "participantSetsInfo": [
                    {
                        "memberInfos": [{"email": r.email}],
                        "order": i + 1,
                        "role": "SIGNER",
                    }
                    for i, r in enumerate(request.recipients)
                ],
                "signatureType": "ESIGN",
                "state": "IN_PROCESS",
                "message": request.message,
            }
            resp = await client.post(
                f"{self._api_base}/agreements", json=payload, headers=self._headers
            )
            resp.raise_for_status()
            data = resp.json()
            return ESignatureEnvelope(
                provider=self.name,
                document_id=request.document_id,
                status=ESignatureStatus.SENT,
                provider_envelope_id=data.get("id"),
                sent_at=datetime.now(UTC),
            )

    async def get(self, envelope_id: str) -> ESignatureEnvelope | None:
        async with await self._client() as client:
            resp = await client.get(f"{self._api_base}/agreements/{envelope_id}", headers=self._headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return ESignatureEnvelope(
                provider=self.name,
                document_id="",
                status=_map_status(data.get("status", "IN_PROCESS")),
                provider_envelope_id=envelope_id,
            )

    async def cancel(self, envelope_id: str) -> bool:
        async with await self._client() as client:
            resp = await client.put(
                f"{self._api_base}/agreements/{envelope_id}/state",
                json={"state": "CANCELLED", "agreementCancellationInfo": {"comment": "cancelled by app"}},
                headers=self._headers,
            )
            return resp.status_code in (200, 204)


def _map_status(value: str) -> ESignatureStatus:
    return {
        "OUT_FOR_SIGNATURE": ESignatureStatus.SENT,
        "WAITING_FOR_MY_SIGNATURE": ESignatureStatus.SENT,
        "SIGNED": ESignatureStatus.SIGNED,
        "COMPLETED": ESignatureStatus.SIGNED,
        "CANCELLED": ESignatureStatus.DECLINED,
        "DECLINED": ESignatureStatus.DECLINED,
        "EXPIRED": ESignatureStatus.EXPIRED,
        "DRAFT": ESignatureStatus.DRAFT,
    }.get(value.upper(), ESignatureStatus.SENT)
