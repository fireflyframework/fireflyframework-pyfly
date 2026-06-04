# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""SMTP email provider — uses the stdlib :mod:`smtplib` from a thread."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage as _EmailMessage

from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult


class SmtpEmailProvider:
    name = "smtp"

    def __init__(
        self,
        host: str,
        *,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls

    async def send(self, message: EmailMessage) -> NotificationResult:
        return await asyncio.get_event_loop().run_in_executor(None, self._send_blocking, message)

    def _send_blocking(self, message: EmailMessage) -> NotificationResult:
        import smtplib

        msg = _EmailMessage()
        msg["From"] = message.sender
        msg["To"] = ", ".join(message.to)
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)
        # ``send_message`` reads recipients from the To/Cc/Bcc headers and
        # strips the Bcc header before transmission, so BCC recipients are
        # delivered without being exposed to other recipients.
        if message.bcc:
            msg["Bcc"] = ", ".join(message.bcc)
        msg["Subject"] = message.subject
        if message.body_text:
            msg.set_content(message.body_text)
        if message.body_html:
            msg.add_alternative(message.body_html, subtype="html")

        # Attach files (audit #31) — EmailMessage promotes to multipart/mixed.
        for attachment in message.attachments:
            maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            msg.add_attachment(
                attachment.data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        # Custom headers (audit #35), without clobbering the standard ones.
        _reserved = {"from", "to", "cc", "bcc", "subject"}
        for key, value in message.headers.items():
            if key.lower() not in _reserved:
                msg[key] = value

        try:
            with smtplib.SMTP(self._host, self._port, timeout=30) as server:
                if self._use_tls:
                    server.starttls()
                if self._username and self._password:
                    server.login(self._username, self._password)
                server.send_message(msg)
            return NotificationResult(id=message.id, provider=self.name, status=EmailStatus.SENT)
        except Exception as exc:  # noqa: BLE001
            return NotificationResult(
                id=message.id,
                provider=self.name,
                status=EmailStatus.FAILED,
                error=str(exc),
            )
