# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly notifications — email / SMS / push abstractions with adapter pattern."""

from __future__ import annotations

from pyfly.notifications.models import (
    Attachment,
    EmailMessage,
    EmailStatus,
    NotificationResult,
    PushMessage,
    SmsMessage,
)
from pyfly.notifications.ports import (
    EmailProvider,
    EmailService,
    PushProvider,
    PushService,
    SmsProvider,
    SmsService,
)
from pyfly.notifications.providers.dummy import (
    DummyEmailProvider,
    DummyPushProvider,
    DummySmsProvider,
)
from pyfly.notifications.providers.firebase import FirebasePushProvider
from pyfly.notifications.providers.resend import ResendEmailProvider
from pyfly.notifications.providers.sendgrid import SendGridEmailProvider
from pyfly.notifications.providers.smtp import SmtpEmailProvider
from pyfly.notifications.providers.twilio import TwilioSmsProvider
from pyfly.notifications.services import (
    DefaultEmailService,
    DefaultPushService,
    DefaultSmsService,
)

__all__ = [
    "Attachment",
    "DefaultEmailService",
    "DefaultPushService",
    "DefaultSmsService",
    "DummyEmailProvider",
    "DummyPushProvider",
    "DummySmsProvider",
    "EmailMessage",
    "EmailProvider",
    "EmailService",
    "EmailStatus",
    "FirebasePushProvider",
    "NotificationResult",
    "PushMessage",
    "PushProvider",
    "PushService",
    "ResendEmailProvider",
    "SendGridEmailProvider",
    "SmsMessage",
    "SmsProvider",
    "SmsService",
    "SmtpEmailProvider",
    "TwilioSmsProvider",
]
