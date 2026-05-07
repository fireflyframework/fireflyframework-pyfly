# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Dummy / in-memory notification providers — perfect for tests + dev."""

from __future__ import annotations

import logging

from pyfly.notifications.models import (
    EmailMessage,
    EmailStatus,
    NotificationResult,
    PushMessage,
    SmsMessage,
)

_logger = logging.getLogger(__name__)


class DummyEmailProvider:
    name = "dummy"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> NotificationResult:
        self.sent.append(message)
        _logger.info("[dummy email] to=%s subject=%s", message.to, message.subject)
        return NotificationResult(
            id=message.id, provider=self.name, status=EmailStatus.SENT, provider_id=message.id
        )


class DummySmsProvider:
    name = "dummy"

    def __init__(self) -> None:
        self.sent: list[SmsMessage] = []

    async def send(self, message: SmsMessage) -> NotificationResult:
        self.sent.append(message)
        _logger.info("[dummy sms] to=%s body=%s", message.to, message.body)
        return NotificationResult(id=message.id, provider=self.name, status=EmailStatus.SENT)


class DummyPushProvider:
    name = "dummy"

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    async def send(self, message: PushMessage) -> NotificationResult:
        self.sent.append(message)
        _logger.info("[dummy push] tokens=%d title=%s", len(message.device_tokens), message.title)
        return NotificationResult(id=message.id, provider=self.name, status=EmailStatus.SENT)
