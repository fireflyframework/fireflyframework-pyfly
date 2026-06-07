# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""DocuSign e-signature adapter — REST API + OAuth JWT bearer."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pyfly.client.pooled import PooledHttpClient
from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    SignatureRequest,
)

_logger = logging.getLogger(__name__)


class DocuSignESignatureAdapter:
    """Bridge to DocuSign's REST API.

    Args:
        base_url: e.g. ``https://demo.docusign.net/restapi``.
        account_id: DocuSign account id.
        access_token: a long-lived OAuth bearer token (caller is responsible for refresh).
    """

    name = "docusign"

    def __init__(
        self,
        *,
        base_url: str,
        account_id: str,
        access_token: str,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._access_token = access_token
        self._http: Any = None

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:  # noqa: BLE001
                msg = "DocuSignESignatureAdapter requires httpx — `pip install pyfly[client]`"
                raise ImportError(msg) from exc
            self._http = httpx.AsyncClient(timeout=60.0)
        return PooledHttpClient(self._http)

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
                "emailSubject": request.subject,
                "emailBlurb": request.message,
                "documents": [
                    {
                        "documentId": request.document_id,
                        "name": "document.pdf",
                        "fileExtension": "pdf",
                    }
                ],
                "recipients": {
                    "signers": [
                        {
                            "email": r.email,
                            "name": r.name,
                            "recipientId": str(i + 1),
                            "routingOrder": str(i + 1),
                        }
                        for i, r in enumerate(request.recipients)
                    ]
                },
                "status": "sent",
            }
            resp = await client.post(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes",
                json=payload,
                headers=self._headers,
            )
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
            resp = await client.get(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes/{envelope_id}",
                headers=self._headers,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return ESignatureEnvelope(
                provider=self.name,
                document_id="",
                status=_map_status(data.get("status", "sent")),
                provider_envelope_id=envelope_id,
                sent_at=_parse(data.get("sentDateTime")),
                signed_at=_parse(data.get("completedDateTime")),
            )

    async def cancel(self, envelope_id: str) -> bool:
        async with await self._client() as client:
            resp = await client.put(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes/{envelope_id}",
                json={"status": "voided", "voidedReason": "cancelled by application"},
                headers=self._headers,
            )
            return bool(resp.status_code == 200)

    async def start(self) -> None:
        """No-op — the pooled HTTP client is created lazily on first use."""

    async def stop(self) -> None:
        """Close the pooled HTTP client on shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _map_status(value: str) -> ESignatureStatus:
    return {
        "created": ESignatureStatus.DRAFT,
        "sent": ESignatureStatus.SENT,
        "delivered": ESignatureStatus.SENT,
        "completed": ESignatureStatus.SIGNED,
        "declined": ESignatureStatus.DECLINED,
        "voided": ESignatureStatus.DECLINED,
        "expired": ESignatureStatus.EXPIRED,
    }.get(value.lower(), ESignatureStatus.SENT)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
