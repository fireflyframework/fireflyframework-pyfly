# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Twilio SMS provider — uses HTTP basic auth against ``api.twilio.com``."""

from __future__ import annotations

from typing import Any

from pyfly.client.pooled import PooledHttpClient
from pyfly.notifications.models import EmailStatus, NotificationResult, SmsMessage


class TwilioSmsProvider:
    """Bridge to Twilio's REST API ``/2010-04-01/Accounts/{sid}/Messages.json`` endpoint."""

    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, *, from_number: str | None = None) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._http: Any = None

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx  # type: ignore[import-not-found, unused-ignore]
            except ImportError as exc:  # noqa: BLE001
                msg = "TwilioSmsProvider requires httpx — `pip install pyfly[client]`"
                raise ImportError(msg) from exc
            self._http = httpx.AsyncClient(timeout=30.0)
        return PooledHttpClient(self._http)

    async def send(self, message: SmsMessage) -> NotificationResult:
        async with await self._client() as client:
            from_number = message.sender or self._from
            if not from_number:
                msg = "TwilioSmsProvider needs a sender — set sender on the message or from_number on the provider"
                raise ValueError(msg)
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Messages.json",
                data={"From": from_number, "To": message.to, "Body": message.body},
                auth=(self._sid, self._token),
            )
            if 200 <= resp.status_code < 300:
                provider_id = resp.json().get("sid")
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
