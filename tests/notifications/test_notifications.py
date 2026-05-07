# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the notifications module."""

from __future__ import annotations

import pytest

from pyfly.notifications.models import (
    EmailMessage,
    EmailStatus,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.providers.dummy import (
    DummyEmailProvider,
    DummyPushProvider,
    DummySmsProvider,
)
from pyfly.notifications.services import (
    DefaultEmailService,
    DefaultPushService,
    DefaultSmsService,
)


@pytest.mark.asyncio
async def test_email_round_trip() -> None:
    provider = DummyEmailProvider()
    service = DefaultEmailService(provider=provider)
    msg = EmailMessage(to=["x@example.com"], sender="me@example.com", subject="hi", body_text="hello")
    result = await service.send(msg)
    assert result.status == EmailStatus.SENT
    assert provider.sent and provider.sent[0].subject == "hi"


@pytest.mark.asyncio
async def test_sms_round_trip() -> None:
    provider = DummySmsProvider()
    service = DefaultSmsService(provider=provider)
    result = await service.send(SmsMessage(to="+10000000000", body="hello"))
    assert result.status == EmailStatus.SENT


@pytest.mark.asyncio
async def test_push_round_trip() -> None:
    provider = DummyPushProvider()
    service = DefaultPushService(provider=provider)
    result = await service.send(PushMessage(device_tokens=["a"], title="hi", body="b"))
    assert result.status == EmailStatus.SENT
