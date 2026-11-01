# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Unicode MIME must remain lossless when an SMTP peer accepts only seven-bit DATA."""

from __future__ import annotations

import asyncio
import socket
from email import policy
from email.parser import BytesParser

import pytest

pytest.importorskip("aiosmtpd")
from aiosmtpd.controller import Controller  # noqa: E402

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus  # noqa: E402
from pyfly.notifications.providers.smtp import SmtpEmailProvider  # noqa: E402


@pytest.fixture
def seven_bit_mailbox():
    class Handler:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):  # noqa: N802 - SMTP callback
            self.messages.append(envelope.original_content)
            return "250 Accepted"

    handler = Handler()
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    # decode_data disables 8BITMIME and rejects non-ASCII DATA before invoking the handler.
    controller = Controller(handler, hostname="127.0.0.1", port=port, decode_data=True, enable_SMTPUTF8=False)
    controller.start()
    try:
        yield port, handler.messages
    finally:
        controller.stop()


@pytest.mark.parametrize("kind", ["plain", "html", "alternative"])
@pytest.mark.parametrize("text", ["Review — café €", "Revisión — información €"])
@pytest.mark.parametrize("long_line", [False, True])
async def test_unicode_message_reaches_seven_bit_peer_without_loss(seven_bit_mailbox, kind, text, long_line):
    port, messages = seven_bit_mailbox
    body = text + ("\n" + "Long line with café and € " * 100 if long_line else "")
    html = "<p>" + body + '</p><img src="cid:brand-logo">'
    attachments = [Attachment("revisión.bin", "application/octet-stream", b"\x00\xff\x80fixture")]
    if kind != "plain":
        attachments.append(Attachment("marca.png", "image/png", b"\xff\x80image", "brand-logo", True))
    message = EmailMessage(
        sender="sender@example.test",
        to=["recipient@example.test"],
        bcc=["blind@example.test"],
        subject=text,
        body_text=body if kind != "html" else None,
        body_html=html if kind != "plain" else None,
        attachments=attachments,
    )
    result = await SmtpEmailProvider("127.0.0.1", port=port, use_tls=False).send(message)
    assert result.status == EmailStatus.SENT
    await asyncio.sleep(0)
    assert len(messages) == 1 and messages[0].isascii()
    assert max(map(len, messages[0].split(b"\r\n"))) <= 998
    parsed = BytesParser(policy=policy.default).parsebytes(messages[0])
    assert all(not part.defects for part in parsed.walk())
    assert str(parsed["Subject"]) == text and parsed["Bcc"] is None
    assert b"blind@example.test" not in messages[0]
    plain_parts = [part for part in parsed.walk() if part.get_content_type() == "text/plain"]
    html_parts = [part for part in parsed.walk() if part.get_content_type() == "text/html"]
    assert len(plain_parts) == (kind != "html")
    assert len(html_parts) == (kind != "plain")
    for part in plain_parts:
        assert part.get_content().replace("\r\n", "\n").rstrip("\n") == body
    for part in html_parts:
        assert part.get_content().replace("\r\n", "\n").rstrip("\n") == html
    for part in plain_parts + html_parts:
        assert part.get_content_charset() == "utf-8"
        assert part["Content-Transfer-Encoding"] in {"quoted-printable", "base64"}
    ordinary = [part for part in parsed.walk() if part.get_content_disposition() == "attachment"]
    assert len(ordinary) == 1 and ordinary[0].get_filename() == "revisión.bin"
    assert ordinary[0].get_payload(decode=True) == b"\x00\xff\x80fixture"
    inline = [part for part in parsed.walk() if part["Content-ID"]]
    assert len(inline) == (kind != "plain")
    for part in inline:
        assert part["Content-ID"] == "<brand-logo>"
        assert part.get_payload(decode=True) == b"\xff\x80image"
        assert part.get_content_disposition() == "inline"
