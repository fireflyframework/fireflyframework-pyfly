# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Resend email provider — HTTPS POST to ``api.resend.com/emails``."""

from __future__ import annotations

from typing import Any

from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult


class ResendEmailProvider:
    """Bridge to Resend's REST API."""

    name = "resend"

    def __init__(self, api_key: str, *, api_base: str = "https://api.resend.com") -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "ResendEmailProvider requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(timeout=30.0)

    async def send(self, message: EmailMessage) -> NotificationResult:
        async with await self._client() as client:
            payload: dict[str, Any] = {
                "from": message.sender,
                "to": message.to,
                "subject": message.subject,
            }
            if message.cc:
                payload["cc"] = message.cc
            if message.bcc:
                payload["bcc"] = message.bcc
            if message.body_text:
                payload["text"] = message.body_text
            if message.body_html:
                payload["html"] = message.body_html
            if message.attachments:
                import base64

                payload["attachments"] = [
                    {
                        "filename": a.filename,
                        "content": base64.b64encode(a.data).decode("ascii"),
                    }
                    for a in message.attachments
                ]
            resp = await client.post(
                f"{self._api_base}/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            if 200 <= resp.status_code < 300:
                provider_id = resp.json().get("id")
                return NotificationResult(
                    id=message.id, provider=self.name, status=EmailStatus.SENT, provider_id=provider_id
                )
            return NotificationResult(
                id=message.id,
                provider=self.name,
                status=EmailStatus.FAILED,
                error=f"http {resp.status_code}: {resp.text}",
            )
