# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Verified SMTP with explicit acceptance and uncertain-outcome semantics."""

from __future__ import annotations

import asyncio
import math
import smtplib
import socket
import ssl
import threading
from contextlib import suppress
from email.message import EmailMessage as _EmailMessage
from typing import Literal

from pyfly.notifications._email import message_headers
from pyfly.notifications.capabilities import EmailProviderCapabilities
from pyfly.notifications.models import EmailMessage, EmailStatus, NotificationResult


class _SmtpOperation:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._connection: smtplib.SMTP | None = None

    def bind(self, connection: smtplib.SMTP) -> None:
        with self._lock:
            self._connection = connection
        self.check()

    def check(self) -> None:
        with self._lock:
            cancelled = self._cancelled
        if cancelled:
            raise ConnectionAbortedError("SMTP operation cancelled")

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            connection = self._connection
        sock = connection.sock if connection is not None else None
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


class SmtpEmailProvider:
    name = "smtp"
    capabilities = EmailProviderCapabilities(real_delivery=True, inline_cid=True)

    def __init__(
        self,
        host: str,
        *,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        tls_mode: Literal["starttls", "implicit", "none"] | None = None,
        timeout: float = 30,
    ) -> None:
        if not isinstance(host, str) or not host or any(c in host for c in "\r\n\0"):
            raise ValueError("SMTP requires a valid host")
        if type(port) is not int or not 1 <= port <= 65535 or type(use_tls) is not bool:
            raise ValueError("SMTP requires a bounded port and explicit TLS setting")
        mode = tls_mode or ("starttls" if use_tls else "none")
        if mode not in {"starttls", "implicit", "none"} or (not use_tls and mode != "none"):
            raise ValueError("Conflicting SMTP TLS settings")
        if bool(username) != bool(password):
            raise ValueError("SMTP username and password must be configured together")
        if username and mode == "none":
            raise ValueError("SMTP credentials require verified TLS")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise ValueError("SMTP timeout must be finite and bounded")
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._validate_context(context)
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._tls_mode, self._context, self._timeout = mode, context, timeout

    @staticmethod
    def _validate_context(context: ssl.SSLContext) -> None:
        if (
            not isinstance(context, ssl.SSLContext)
            or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
        ):
            raise ValueError("SMTP TLS requires certificate and hostname verification")

    async def send(self, message: EmailMessage) -> NotificationResult:
        operation = _SmtpOperation()
        sending = asyncio.get_running_loop().run_in_executor(None, self._send_blocking, message, operation)
        try:
            return await asyncio.shield(sending)
        except asyncio.CancelledError:
            operation.cancel()
            # Keep the caller's lease/admission until the owned socket and thread finish.
            # DATA may already be accepted; cancellation is never permission to replay it.
            while not sending.done():
                try:
                    await asyncio.shield(sending)
                except asyncio.CancelledError:
                    operation.cancel()
            sending.result()
            raise

    @staticmethod
    def _mime(message: EmailMessage) -> _EmailMessage:
        msg = _EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = message.sender, ", ".join(message.to), message.subject
        if message.cc:
            msg["Cc"] = ", ".join(message.cc)
        if message.bcc:
            msg["Bcc"] = ", ".join(message.bcc)
        if message.body_text:
            msg.set_content(message.body_text)
        if message.body_html:
            msg.add_alternative(message.body_html, subtype="html")
        for name, value in message_headers(message).items():
            msg[name] = value
        html = msg.get_body(preferencelist=("html",))
        for attachment in message.attachments:
            maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            if attachment.inline:
                if html is None:
                    raise ValueError("Inline attachments require an HTML body")
                html.add_related(
                    attachment.data,
                    cid=f"<{attachment.content_id}>",
                    disposition="inline",
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=attachment.filename,
                )
            else:
                msg.add_attachment(
                    attachment.data,
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=attachment.filename,
                )
        return msg

    def _send_blocking(self, message: EmailMessage, operation: _SmtpOperation) -> NotificationResult:
        try:
            msg = self._mime(message)
            self._validate_context(self._context)
        except Exception:
            return NotificationResult(message.id, self.name, EmailStatus.FAILED, error="EMAIL_PREPARATION_FAILED")
        dispatched, accepted, partial = False, False, False
        try:
            operation.check()
            connection = (
                smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=self._context)
                if self._tls_mode == "implicit"
                else smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            )
            with connection as server:
                operation.bind(server)
                if self._tls_mode == "starttls":
                    server.starttls(context=self._context)
                operation.check()
                if self._username and self._password:
                    server.login(self._username, self._password)
                operation.check()
                dispatched = True
                refused = server.send_message(msg)
                partial = bool(refused)
                accepted = not partial
        except Exception as error:
            if accepted:
                return NotificationResult(message.id, self.name, EmailStatus.SENT, provider_id=str(msg["Message-ID"]))
            if partial:
                return NotificationResult(
                    message.id,
                    self.name,
                    EmailStatus.UNKNOWN,
                    provider_id=str(msg["Message-ID"]),
                    error="EMAIL_PARTIAL_ACCEPTANCE",
                )
            known_rejection = isinstance(
                error,
                (
                    smtplib.SMTPAuthenticationError,
                    smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPSenderRefused,
                    smtplib.SMTPDataError,
                ),
            )
            status = EmailStatus.UNKNOWN if dispatched and not known_rejection else EmailStatus.FAILED
            code = (
                "EMAIL_SEND_OUTCOME_UNKNOWN"
                if status == EmailStatus.UNKNOWN
                else "SMTP_SEND_REJECTED"
                if known_rejection
                else "SMTP_TLS_FAILED"
                if isinstance(error, ssl.SSLError)
                else "SMTP_CONNECTION_FAILED"
            )
            return NotificationResult(
                message.id, self.name, status, provider_id=str(msg["Message-ID"]) if dispatched else None, error=code
            )
        if partial:
            return NotificationResult(
                message.id,
                self.name,
                EmailStatus.UNKNOWN,
                provider_id=str(msg["Message-ID"]),
                error="EMAIL_PARTIAL_ACCEPTANCE",
            )
        return NotificationResult(message.id, self.name, EmailStatus.SENT, provider_id=str(msg["Message-ID"]))
