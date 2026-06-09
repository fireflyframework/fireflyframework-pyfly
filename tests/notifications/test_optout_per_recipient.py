# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Opt-out must apply to EVERY recipient (to/cc/bcc + every push token), not just the
first — and must match regardless of casing/formatting. Regression for the commit
security review (opt-out / CAN-SPAM / GDPR bypass)."""

from __future__ import annotations

import pytest

from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult, PushMessage
from pyfly.notifications.preferences import InMemoryPreferenceService
from pyfly.notifications.services import DefaultEmailService, DefaultPushService


class _RecordingEmailProvider:
    name = "rec-email"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> NotificationResult:
        self.sent.append(message)
        return NotificationResult(id=message.id, provider=self.name, status=EmailStatus.SENT)


class _RecordingPushProvider:
    name = "rec-push"

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    async def send(self, message: PushMessage) -> NotificationResult:
        self.sent.append(message)
        return NotificationResult(id=message.id, provider=self.name, status=EmailStatus.SENT)


@pytest.mark.asyncio
async def test_email_optout_prunes_cc_and_bcc_not_just_first() -> None:
    provider = _RecordingEmailProvider()
    prefs = InMemoryPreferenceService()
    prefs.opt_out("blocked@x.com", "email")
    service = DefaultEmailService(provider, preference_service=prefs)

    result = await service.send(
        EmailMessage(
            to=["alice@x.com", "blocked@x.com"],
            cc=["blocked@x.com", "carol@x.com"],
            bcc=["blocked@x.com"],
            sender="me@x.com",
            subject="hi",
            body_text="body",
        )
    )

    assert result.status == EmailStatus.SENT
    delivered = provider.sent[0]
    # The opted-out address is pruned from ALL recipient lists; the provider never sees it.
    assert delivered.to == ["alice@x.com"]
    assert delivered.cc == ["carol@x.com"]
    assert delivered.bcc == []


@pytest.mark.asyncio
async def test_email_all_recipients_optout_suppresses_and_skips_provider() -> None:
    provider = _RecordingEmailProvider()
    prefs = InMemoryPreferenceService()
    prefs.opt_out("a@x.com", "email")
    prefs.opt_out("b@x.com", "email")
    service = DefaultEmailService(provider, preference_service=prefs)

    result = await service.send(
        EmailMessage(to=["a@x.com"], cc=["b@x.com"], sender="me@x.com", subject="s", body_text="b")
    )

    assert result.status == EmailStatus.SUPPRESSED
    assert provider.sent == []  # provider never called


@pytest.mark.asyncio
async def test_push_optout_prunes_individual_tokens() -> None:
    provider = _RecordingPushProvider()
    prefs = InMemoryPreferenceService()
    prefs.opt_out("tok-bad", "push")
    service = DefaultPushService(provider, preference_service=prefs)

    result = await service.send(PushMessage(device_tokens=["tok-good", "tok-bad"], title="t", body="b"))

    assert result.status == EmailStatus.SENT
    assert provider.sent[0].device_tokens == ["tok-good"]


@pytest.mark.asyncio
async def test_push_all_tokens_optout_suppresses() -> None:
    provider = _RecordingPushProvider()
    prefs = InMemoryPreferenceService()
    prefs.opt_out("tok-1", "push")
    service = DefaultPushService(provider, preference_service=prefs)

    result = await service.send(PushMessage(device_tokens=["tok-1"], title="t", body="b"))

    assert result.status == EmailStatus.SUPPRESSED
    assert provider.sent == []


@pytest.mark.asyncio
async def test_email_optout_is_case_insensitive() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("Alice@X.com", "email")
    assert await prefs.is_opted_in("alice@x.com", "email") is False
    assert await prefs.is_opted_in("  ALICE@x.COM ", "email") is False
    assert await prefs.is_opted_in("bob@x.com", "email") is True


@pytest.mark.asyncio
async def test_sms_optout_normalizes_phone_formatting() -> None:
    prefs = InMemoryPreferenceService()
    prefs.opt_out("+1 (555) 123-4567", "sms")
    assert await prefs.is_opted_in("+15551234567", "sms") is False
