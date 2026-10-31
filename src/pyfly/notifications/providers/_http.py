# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Email HTTP outcomes without response bodies or implicit transport replays."""

from typing import Any

from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult


async def dispatch_http_email(
    provider: Any, message: EmailMessage, payload: dict[str, Any], path: str, *, id_header: str | None = None
) -> NotificationResult:
    dispatched = False
    try:
        async with await provider._client() as client:
            dispatched = True
            response = await client.post(
                provider._api_base + path,
                json=payload,
                headers={"Authorization": f"Bearer {provider._api_key}", "Content-Type": "application/json"},
            )
            if 200 <= response.status_code < 300:
                provider_id = response.headers.get(id_header) if id_header else response.json().get("id")
                return NotificationResult(message.id, provider.name, EmailStatus.SENT, provider_id=provider_id)
            status = EmailStatus.UNKNOWN if response.status_code >= 500 else EmailStatus.FAILED
            return NotificationResult(message.id, provider.name, status, error=f"EMAIL_HTTP_{response.status_code}")
    except Exception as error:
        known_unsent = not dispatched
        if dispatched:
            try:
                import httpx
            except ImportError:
                pass
            else:
                known_unsent = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
        return NotificationResult(
            message.id,
            provider.name,
            EmailStatus.FAILED if known_unsent else EmailStatus.UNKNOWN,
            error="EMAIL_CONNECTION_FAILED" if known_unsent else "EMAIL_SEND_OUTCOME_UNKNOWN",
        )
