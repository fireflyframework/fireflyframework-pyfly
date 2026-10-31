# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Resend email provider — HTTPS POST to ``api.resend.com/emails``."""

from __future__ import annotations

from typing import Any

from pyfly.client.pooled import PooledHttpClient
from pyfly.notifications._email import message_headers, secure_endpoint
from pyfly.notifications.capabilities import EmailProviderCapabilities
from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult
from pyfly.notifications.providers._http import dispatch_http_email


class ResendEmailProvider:
    """Bridge to Resend's REST API."""

    name = "resend"
    capabilities = EmailProviderCapabilities(real_delivery=True, inline_cid=True)

    def __init__(
        self, api_key: str, *, api_base: str = "https://api.resend.com", default_from: str | None = None
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Email API credential must be configured")
        self._api_key = api_key
        self._api_base = secure_endpoint(api_base)
        self._default_from = default_from
        self._http: Any = None

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:  # noqa: BLE001
                msg = "ResendEmailProvider requires httpx — `pip install pyfly[client]`"
                raise ImportError(msg) from exc
            self._http = httpx.AsyncClient(timeout=30.0)
        return PooledHttpClient(self._http)

    async def send(self, message: EmailMessage) -> NotificationResult:
        try:
            payload = self._payload(message)
        except Exception:
            return NotificationResult(message.id, self.name, EmailStatus.FAILED, error="EMAIL_PREPARATION_FAILED")
        return await dispatch_http_email(self, message, payload, "/emails")

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "headers": message_headers(message),
            "from": message.sender or self._default_from,
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
                    **({"content_id": a.content_id} if a.inline else {}),
                }
                for a in message.attachments
            ]
        return payload

    async def start(self) -> None:
        """No-op — the pooled HTTP client is created lazily on first use."""

    async def stop(self) -> None:
        """Close the pooled HTTP client on shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
