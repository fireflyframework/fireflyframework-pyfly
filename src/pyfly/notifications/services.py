# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Default notification services that delegate to a provider adapter."""

from __future__ import annotations

from typing import Any, cast

from pyfly.notifications.models import (
    EmailMessage,
    EmailStatus,
    NotificationResult,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.ports import (
    EmailProvider,
    PushProvider,
    SmsProvider,
)


async def _send_safely(provider: Any, message: Any) -> NotificationResult:
    """Delegate to the provider, converting an exception into a FAILED result.

    Provider exceptions become a structured FAILED NotificationResult rather
    than propagating to the caller (audit #36), matching the Java contract.
    """
    try:
        return cast(NotificationResult, await provider.send(message))
    except Exception as exc:  # noqa: BLE001
        return NotificationResult(
            id=message.id,
            provider=getattr(provider, "name", "unknown"),
            status=EmailStatus.FAILED,
            error=str(exc),
        )


class DefaultEmailService:
    def __init__(self, provider: EmailProvider) -> None:
        self._provider = provider

    async def send(self, message: EmailMessage) -> NotificationResult:
        return await _send_safely(self._provider, message)


class DefaultSmsService:
    def __init__(self, provider: SmsProvider) -> None:
        self._provider = provider

    async def send(self, message: SmsMessage) -> NotificationResult:
        return await _send_safely(self._provider, message)


class DefaultPushService:
    def __init__(self, provider: PushProvider) -> None:
        self._provider = provider

    async def send(self, message: PushMessage) -> NotificationResult:
        return await _send_safely(self._provider, message)
