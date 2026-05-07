# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Built-in notification provider adapters."""

from __future__ import annotations

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

__all__ = [
    "DummyEmailProvider",
    "DummyPushProvider",
    "DummySmsProvider",
    "FirebasePushProvider",
    "ResendEmailProvider",
    "SendGridEmailProvider",
    "SmtpEmailProvider",
    "TwilioSmsProvider",
]
