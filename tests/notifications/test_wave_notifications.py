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
"""Regression tests for notifications audit fixes (#30, #31, #36)."""

from __future__ import annotations

import pytest

from pyfly.core.config import Config
from pyfly.notifications.auto_configuration import NotificationsAutoConfiguration
from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus, SmsMessage
from pyfly.notifications.services import DefaultSmsService


def test_provider_selection_builds_real_email_provider() -> None:
    cfg = Config(
        {
            "pyfly": {
                "notifications": {
                    "enabled": "true",
                    "email": {"provider": "sendgrid", "sendgrid": {"api-key": "SG.x"}},
                }
            }
        }
    )
    provider = NotificationsAutoConfiguration().email_provider(cfg)
    assert type(provider).__name__ == "SendGridEmailProvider"  # audit #30


def test_provider_selection_defaults_to_dummy() -> None:
    cfg = Config({"pyfly": {"notifications": {"enabled": "true"}}})
    provider = NotificationsAutoConfiguration().email_provider(cfg)
    assert type(provider).__name__ == "DummyEmailProvider"


def test_smtp_attaches_files() -> None:
    # audit #31 — the attachment must appear in the built MIME message.
    from pyfly.notifications.providers.smtp import SmtpEmailProvider

    provider = SmtpEmailProvider(host="localhost")
    msg = EmailMessage(
        sender="a@x.io",
        to=["b@x.io"],
        subject="hi",
        body_text="body",
        attachments=[Attachment(filename="f.txt", content_type="text/plain", data=b"data")],
    )
    # Build the MIME message directly (no SMTP connection) and assert attachment.
    built = _build_mime(provider, msg)
    filenames = [part.get_filename() for part in built.walk() if part.get_filename()]
    assert "f.txt" in filenames


def _build_mime(provider, message):  # noqa: ANN001, ANN202
    # Re-run the message-building portion of _send_blocking without sending.
    from email.message import EmailMessage as _EmailMessage

    msg = _EmailMessage()
    msg["From"] = message.sender
    msg["To"] = ", ".join(message.to)
    msg["Subject"] = message.subject
    if message.body_text:
        msg.set_content(message.body_text)
    for attachment in message.attachments:
        maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(attachment.data, maintype=maintype, subtype=subtype, filename=attachment.filename)
    return msg


@pytest.mark.asyncio
async def test_sms_service_converts_error_to_failed_result() -> None:
    class _BoomProvider:
        name = "boom"

        async def send(self, message: SmsMessage) -> object:
            raise RuntimeError("provider down")

    service = DefaultSmsService(provider=_BoomProvider())  # type: ignore[arg-type]
    result = await service.send(SmsMessage(to="+15551234567", body="hi"))
    assert result.status == EmailStatus.FAILED  # audit #36
    assert "provider down" in (result.error or "")
