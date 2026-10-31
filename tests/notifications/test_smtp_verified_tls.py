# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Actual STARTTLS against an owned CA; no preexisting SMTP service is contacted."""

from __future__ import annotations

import socket
import ssl

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message
from aiosmtpd.smtp import AuthResult

from pyfly.notifications.models import EmailMessage, EmailStatus
from pyfly.notifications.providers.smtp import SmtpEmailProvider
from tests.client.test_tls_handshake import certificates as certificates


@pytest.fixture
def tls_mailbox(certificates):
    authenticated = []

    class Capture(Message):
        def __init__(self):
            super().__init__()
            self.messages = []

        def handle_message(self, message):
            self.messages.append(message)

    def authenticate(server, session, envelope, mechanism, auth_data):
        authenticated.append(True)
        return AuthResult(success=True)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates["server"][2], certificates["server"][3])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = Capture()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        tls_context=context,
        require_starttls=True,
        auth_required=True,
        authenticator=authenticate,
    )
    controller.start()
    try:
        yield handler, port, authenticated
    finally:
        controller.stop()


async def test_default_starttls_rejects_unknown_ca_before_credentials_and_data(tls_mailbox):
    handler, port, authenticated = tls_mailbox
    provider = SmtpEmailProvider("127.0.0.1", port=port, username="owned-fixture", password="synthetic-password")
    result = await provider.send(
        EmailMessage(
            sender="sender@example.test",
            to=["recipient@example.test"],
            subject="owned fixture",
            body_text="synthetic private body",
        )
    )
    assert result.status == EmailStatus.FAILED, (
        f"Untrusted peer accepted authentication={len(authenticated)} DATA={len(handler.messages)}"
    )
    assert not authenticated and not handler.messages


async def test_trusted_starttls_carries_related_inline_cid_and_stable_message_id(tls_mailbox, certificates):
    from pyfly.notifications.models import Attachment

    handler, port, authenticated = tls_mailbox
    context = ssl.create_default_context(cafile=certificates["ca"][2])
    provider = SmtpEmailProvider(
        "127.0.0.1", port=port, username="owned-fixture", password="synthetic-password", ssl_context=context
    )
    message = EmailMessage(
        id="stable-owned-message",
        sender="sender@example.test",
        to=["recipient@example.test"],
        bcc=["blind@example.test"],
        subject="owned fixture",
        body_text="fallback",
        body_html='<img src="cid:brand-logo">',
        attachments=[Attachment("brand.png", "image/png", b"synthetic-image", content_id="brand-logo", inline=True)],
    )
    for _ in range(2):
        assert (await provider.send(message)).status == EmailStatus.SENT
    assert len(authenticated) == len(handler.messages) == 2
    first, second = handler.messages
    assert first["Bcc"] is None
    assert first["Message-ID"] == second["Message-ID"] and first["Message-ID"]
    images = [part for part in first.walk() if part["Content-ID"]]
    assert len(images) == 1 and images[0]["Content-ID"] == "<brand-logo>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_payload(decode=True) == b"synthetic-image"


def test_plaintext_configuration_cannot_send_credentials():
    with pytest.raises(ValueError):
        SmtpEmailProvider("localhost", username="owned-fixture", password="synthetic-password", use_tls=False)


def test_unverified_injected_context_is_rejected_before_opening_any_socket():
    context = ssl._create_unverified_context()
    with pytest.raises(ValueError):
        SmtpEmailProvider("localhost", ssl_context=context)


@pytest.fixture
def implicit_mailbox(certificates):
    class Capture(Message):
        def __init__(self):
            super().__init__()
            self.messages = []

        def handle_message(self, message):
            self.messages.append(message)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates["server"][2], certificates["server"][3])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = Capture()
    controller = Controller(handler, hostname="127.0.0.1", port=port, ssl_context=context)
    controller.start()
    try:
        yield handler, port
    finally:
        controller.stop()


async def test_implicit_tls_verifies_trust_and_hostname(implicit_mailbox, certificates):
    handler, port = implicit_mailbox
    message = EmailMessage(sender="sender@example.test", to=["recipient@example.test"], body_text="owned fixture")
    denied = await SmtpEmailProvider("127.0.0.1", port=port, tls_mode="implicit").send(message)
    assert denied.status == EmailStatus.FAILED and denied.error == "SMTP_TLS_FAILED" and not handler.messages
    trusted = ssl.create_default_context(cafile=certificates["ca"][2])
    mismatch = await SmtpEmailProvider("localhost", port=port, tls_mode="implicit", ssl_context=trusted).send(message)
    assert mismatch.status == EmailStatus.FAILED and mismatch.error == "SMTP_TLS_FAILED" and not handler.messages
    accepted = await SmtpEmailProvider("127.0.0.1", port=port, tls_mode="implicit", ssl_context=trusted).send(message)
    assert accepted.status == EmailStatus.SENT and len(handler.messages) == 1


async def test_multiple_inline_images_share_one_related_html_container(tls_mailbox, certificates):
    from pyfly.notifications.models import Attachment

    handler, port, _ = tls_mailbox
    provider = SmtpEmailProvider(
        "127.0.0.1",
        port=port,
        username="owned-fixture",
        password="synthetic-password",
        ssl_context=ssl.create_default_context(cafile=certificates["ca"][2]),
    )
    result = await provider.send(
        EmailMessage(
            sender="sender@example.test",
            to=["recipient@example.test"],
            body_html='<img src="cid:one"><img src="cid:two">',
            attachments=[
                Attachment("one.png", "image/png", b"one", "one", True),
                Attachment("two.png", "image/png", b"two", "two", True),
            ],
        )
    )
    assert result.status == EmailStatus.SENT
    related = [part for part in handler.messages[0].walk() if part.get_content_type() == "multipart/related"]
    assert len(related) == 1, "All referenced images belong to the same HTML resource scope"


async def test_partial_recipient_acceptance_is_unknown_and_never_replayed():
    class Partial(Message):
        def __init__(self):
            super().__init__()
            self.messages = []

        async def handle_RCPT(self, server, session, envelope, address, options):  # noqa: N802 - SMTP protocol hook
            if address.startswith("refused"):
                return "550 recipient refused"
            envelope.rcpt_tos.append(address)
            return "250 accepted"

        def handle_message(self, message):
            self.messages.append(message)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = Partial()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        result = await SmtpEmailProvider("127.0.0.1", port=port, use_tls=False).send(
            EmailMessage(
                sender="sender@example.test",
                to=["accepted@example.test", "refused@example.test"],
                body_text="owned fixture",
            )
        )
        assert result.status == EmailStatus.UNKNOWN and result.error == "EMAIL_PARTIAL_ACCEPTANCE"
        assert len(handler.messages) == 1
    finally:
        controller.stop()


async def test_qualified_native_tls_context_reaches_real_smtp_provider(tls_mailbox, certificates):
    from pyfly.context import ApplicationContext
    from pyfly.core import Config
    from pyfly.notifications import DefaultEmailService

    handler, port, _ = tls_mailbox
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "notifications": {
                        "enabled": True,
                        "email": {
                            "provider": "smtp",
                            "smtp": {
                                "host": "127.0.0.1",
                                "port": port,
                                "username": "owned-fixture",
                                "password": "synthetic-password",
                            },
                        },
                    }
                }
            }
        )
    )
    context.container.register_instance(
        ssl.SSLContext, ssl.create_default_context(cafile=certificates["ca"][2]), name="smtp_email_ssl_context"
    )
    await context.start()
    try:
        result = await context.get_bean(DefaultEmailService).send(
            EmailMessage(sender="sender@example.test", to=["recipient@example.test"], body_text="owned fixture")
        )
        assert result.status == EmailStatus.SENT and len(handler.messages) == 1
    finally:
        await context.stop()


async def test_cancelled_send_drains_owned_smtp_thread_before_releasing_caller(monkeypatch):
    import asyncio
    import threading

    entered, allow_response, worker_finished, allow_finish = (threading.Event() for _ in range(4))

    class PausedData:
        calls = 0

        async def handle_DATA(self, server, session, envelope):  # noqa: N802 - SMTP protocol hook
            self.calls += 1
            entered.set()
            while not allow_response.is_set():
                await asyncio.sleep(0.005)
            return "250 accepted"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = PausedData()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    provider = SmtpEmailProvider("127.0.0.1", port=port, use_tls=False, timeout=0.3)
    original = provider._send_blocking

    def tracked(*args):
        try:
            return original(*args)
        finally:
            allow_finish.wait(2)
            worker_finished.set()

    monkeypatch.setattr(provider, "_send_blocking", tracked)
    task = asyncio.create_task(provider.send(EmailMessage(sender="sender@example.test", to=["one@example.test"])))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.03)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done(), "Cancellation must retain the caller's lease/admission until its SMTP thread drains"
        allow_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert worker_finished.is_set() and handler.calls == 1
    finally:
        allow_finish.set()
        allow_response.set()
        await asyncio.gather(task, return_exceptions=True)
        controller.stop()
