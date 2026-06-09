# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for SmtpEmailProvider using an in-process aiosmtpd server.

An in-process SMTP server is started on an ephemeral port; the real
SmtpEmailProvider (smtplib + thread-executor) connects to it.  We verify that
the MIME message delivered to the server matches the EmailMessage we built —
without any mocks on the provider itself.

``aiosmtpd`` is a dev dependency; tests are skipped when it is absent.
"""

from __future__ import annotations

import asyncio
import email
import socket
from typing import Any

import pytest

aiosmtpd = pytest.importorskip("aiosmtpd")

from aiosmtpd.controller import Controller  # noqa: E402
from aiosmtpd.handlers import Message  # noqa: E402

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus  # noqa: E402
from pyfly.notifications.providers.smtp import SmtpEmailProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class CapturingHandler(Message):
    """aiosmtpd handler that stores every message that passes through."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[email.message.Message] = []

    def handle_message(self, message: email.message.Message) -> None:
        self.messages.append(message)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def smtp_server() -> Any:
    """Start an aiosmtpd Controller on an ephemeral port; yield (handler, port); stop on teardown."""
    handler = CapturingHandler()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    yield handler, port
    controller.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_delivers_plain_text(smtp_server: Any) -> None:
    """A plain-text EmailMessage is delivered with the correct From/To/Subject/body."""
    handler, port = smtp_server
    provider = SmtpEmailProvider(host="127.0.0.1", port=port, use_tls=False)

    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="Hello SMTP",
        body_text="plain text body",
    )
    result = await provider.send(msg)

    assert result.status == EmailStatus.SENT
    assert result.provider == "smtp"
    assert result.error is None

    # give the in-process handler a tick to record the message
    await asyncio.sleep(0.05)
    assert len(handler.messages) == 1
    captured = handler.messages[0]
    assert captured["From"] == "from@example.com"
    assert "dest@example.com" in captured["To"]
    assert captured["Subject"] == "Hello SMTP"
    body = captured.get_payload(decode=True)
    assert body is not None
    assert b"plain text body" in body


@pytest.mark.asyncio
async def test_smtp_delivers_html_body(smtp_server: Any) -> None:
    """An HTML-body message is delivered and contains the HTML content."""
    handler, port = smtp_server
    provider = SmtpEmailProvider(host="127.0.0.1", port=port, use_tls=False)

    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="HTML Test",
        body_text="fallback",
        body_html="<h1>Hello</h1>",
    )
    result = await provider.send(msg)

    assert result.status == EmailStatus.SENT

    await asyncio.sleep(0.05)
    assert len(handler.messages) == 1
    captured = handler.messages[0]
    # walk parts to find html
    html_found = False
    for part in captured.walk():
        ct = part.get_content_type()
        if ct == "text/html":
            payload = part.get_payload(decode=True)
            assert payload is not None
            assert b"<h1>Hello</h1>" in payload
            html_found = True
    assert html_found, "Expected a text/html MIME part"


@pytest.mark.asyncio
async def test_smtp_delivers_attachment(smtp_server: Any) -> None:
    """An EmailMessage with an attachment is delivered with the attachment intact."""
    handler, port = smtp_server
    provider = SmtpEmailProvider(host="127.0.0.1", port=port, use_tls=False)

    raw = b"binary-attachment-content"
    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="Attachment Test",
        body_text="see attached",
        attachments=[Attachment(filename="hello.bin", content_type="application/octet-stream", data=raw)],
    )
    result = await provider.send(msg)

    assert result.status == EmailStatus.SENT

    await asyncio.sleep(0.05)
    assert len(handler.messages) == 1
    captured = handler.messages[0]
    attachment_found = False
    for part in captured.walk():
        if part.get_filename() == "hello.bin":
            payload = part.get_payload(decode=True)
            assert payload == raw
            attachment_found = True
    assert attachment_found, "Expected a 'hello.bin' attachment part"


@pytest.mark.asyncio
async def test_smtp_returns_failed_on_connection_error() -> None:
    """A connection failure (wrong port) maps to a FAILED NotificationResult; does not raise."""
    provider = SmtpEmailProvider(host="127.0.0.1", port=1, use_tls=False)  # port 1 won't accept

    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="Will fail",
        body_text="body",
    )
    result = await provider.send(msg)

    assert result.status == EmailStatus.FAILED
    assert result.error is not None
    assert result.provider_id is None
