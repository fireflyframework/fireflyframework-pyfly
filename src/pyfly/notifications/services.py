# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Default notification services that delegate to a provider adapter."""

from __future__ import annotations

from pyfly.notifications.models import (
    EmailMessage,
    NotificationResult,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.ports import (
    EmailProvider,
    PushProvider,
    SmsProvider,
)


class DefaultEmailService:
    def __init__(self, provider: EmailProvider) -> None:
        self._provider = provider

    async def send(self, message: EmailMessage) -> NotificationResult:
        return await self._provider.send(message)


class DefaultSmsService:
    def __init__(self, provider: SmsProvider) -> None:
        self._provider = provider

    async def send(self, message: SmsMessage) -> NotificationResult:
        return await self._provider.send(message)


class DefaultPushService:
    def __init__(self, provider: PushProvider) -> None:
        self._provider = provider

    async def send(self, message: PushMessage) -> NotificationResult:
        return await self._provider.send(message)
