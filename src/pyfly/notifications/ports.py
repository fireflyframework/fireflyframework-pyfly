# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Notification ports (protocols)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pyfly.notifications.models import (
    EmailMessage,
    NotificationResult,
    PushMessage,
    SmsMessage,
)


@runtime_checkable
class EmailProvider(Protocol):
    name: str

    async def send(self, message: EmailMessage) -> NotificationResult: ...


@runtime_checkable
class SmsProvider(Protocol):
    name: str

    async def send(self, message: SmsMessage) -> NotificationResult: ...


@runtime_checkable
class PushProvider(Protocol):
    name: str

    async def send(self, message: PushMessage) -> NotificationResult: ...


@runtime_checkable
class EmailService(Protocol):
    async def send(self, message: EmailMessage) -> NotificationResult: ...


@runtime_checkable
class SmsService(Protocol):
    async def send(self, message: SmsMessage) -> NotificationResult: ...


@runtime_checkable
class PushService(Protocol):
    async def send(self, message: PushMessage) -> NotificationResult: ...
