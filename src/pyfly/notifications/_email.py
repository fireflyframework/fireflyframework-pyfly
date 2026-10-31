# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native message header construction shared by email transports."""

from email.utils import parseaddr
from uuid import NAMESPACE_URL, uuid5

from pyfly.notifications.models import EmailMessage


def message_headers(message: EmailMessage) -> dict[str, str]:
    reserved = {"from", "to", "cc", "bcc", "subject", "content-type", "content-transfer-encoding", "mime-version"}
    headers = {}
    seen = set()
    for name, value in message.headers.items():
        if not isinstance(name, str) or not name or any(c in name for c in "\r\n\0:"):
            raise ValueError("Invalid email header name")
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise ValueError("Invalid email header value")
        if name.lower() in reserved:
            continue
        if name.lower() in seen:
            raise ValueError("Duplicate email header")
        seen.add(name.lower())
        headers[name] = value
    if "message-id" not in seen:
        domain = parseaddr(message.sender)[1].rpartition("@")[2] or "localhost"
        headers["Message-ID"] = f"<{uuid5(NAMESPACE_URL, 'pyfly-email:' + message.id)}@{domain}>"
    for attachment in message.attachments:
        attachment.__post_init__()
    return headers


def secure_endpoint(value: str) -> str:
    from urllib.parse import urlsplit

    if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
        raise ValueError("Email API endpoint must be an absolute HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Email API endpoint must be HTTPS without credentials, query or fragment")
    _ = parsed.port
    return value.rstrip("/")
