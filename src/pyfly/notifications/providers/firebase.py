# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Firebase Cloud Messaging push provider — HTTP v1 API."""

from __future__ import annotations

from typing import Any

from pyfly.notifications.models import EmailStatus, NotificationResult, PushMessage


class FirebasePushProvider:
    """Bridge to FCM v1 ``/v1/projects/<id>/messages:send`` endpoint.

    Args:
        project_id: GCP project id (FCM uses it in the URL).
        access_token: short-lived OAuth token from your service account.
    """

    name = "firebase"

    def __init__(self, *, project_id: str, access_token: str) -> None:
        self._project_id = project_id
        self._access_token = access_token

    async def _client(self) -> Any:
        try:
            import httpx  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "FirebasePushProvider requires httpx — `pip install pyfly[client]`"
            raise ImportError(msg) from exc
        return httpx.AsyncClient(timeout=30.0)

    async def send(self, message: PushMessage) -> NotificationResult:
        async with await self._client() as client:
            sent_ids: list[str] = []
            errors: list[str] = []
            for token in message.device_tokens:
                resp = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send",
                    json={
                        "message": {
                            "token": token,
                            "notification": {"title": message.title, "body": message.body},
                            "data": {k: str(v) for k, v in message.data.items()},
                        }
                    },
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                if 200 <= resp.status_code < 300:
                    sent_ids.append(resp.json().get("name", ""))
                else:
                    errors.append(f"{token}: http {resp.status_code}")
            if sent_ids and not errors:
                return NotificationResult(
                    id=message.id, provider=self.name, status=EmailStatus.SENT, provider_id=";".join(sent_ids)
                )
            return NotificationResult(
                id=message.id,
                provider=self.name,
                status=EmailStatus.FAILED if not sent_ids else EmailStatus.SENT,
                error="; ".join(errors) or None,
                provider_id=";".join(sent_ids) or None,
            )
