# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""SendGrid email provider — talks to ``api.sendgrid.com`` over HTTPS."""

from __future__ import annotations

import base64
from typing import Any

from pyfly.client.pooled import PooledHttpClient
from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult


class SendGridEmailProvider:
    """Bridge to SendGrid's v3 ``/mail/send`` endpoint."""

    name = "sendgrid"

    def __init__(self, api_key: str, *, api_base: str = "https://api.sendgrid.com/v3") -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._http: Any = None

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:  # noqa: BLE001
                msg = "SendGridEmailProvider requires httpx — `pip install pyfly[client]`"
                raise ImportError(msg) from exc
            self._http = httpx.AsyncClient(timeout=30.0)
        return PooledHttpClient(self._http)

    async def send(self, message: EmailMessage) -> NotificationResult:
        async with await self._client() as client:
            payload: dict[str, Any] = {
                "personalizations": [
                    {
                        "to": [{"email": e} for e in message.to],
                        "cc": [{"email": e} for e in message.cc] or None,
                        "bcc": [{"email": e} for e in message.bcc] or None,
                        "subject": message.subject,
                    }
                ],
                "from": {"email": message.sender},
                "content": [],
            }
            if message.body_text:
                payload["content"].append({"type": "text/plain", "value": message.body_text})
            if message.body_html:
                payload["content"].append({"type": "text/html", "value": message.body_html})
            if message.template_id:
                payload["template_id"] = message.template_id
                payload["personalizations"][0]["dynamic_template_data"] = message.template_data
            if message.attachments:
                payload["attachments"] = [
                    {
                        "filename": a.filename,
                        "type": a.content_type,
                        "content": base64.b64encode(a.data).decode("ascii"),
                    }
                    for a in message.attachments
                ]
            # Drop empty cc/bcc to keep SendGrid happy.
            for k in ("cc", "bcc"):
                if not payload["personalizations"][0].get(k):
                    payload["personalizations"][0].pop(k, None)

            resp = await client.post(
                f"{self._api_base}/mail/send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            if 200 <= resp.status_code < 300:
                provider_id = resp.headers.get("X-Message-Id")
                return NotificationResult(
                    id=message.id, provider=self.name, status=EmailStatus.SENT, provider_id=provider_id
                )
            return NotificationResult(
                id=message.id,
                provider=self.name,
                status=EmailStatus.FAILED,
                error=f"http {resp.status_code}: {resp.text}",
            )

    async def start(self) -> None:
        """No-op — the pooled HTTP client is created lazily on first use."""

    async def stop(self) -> None:
        """Close the pooled HTTP client on shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
