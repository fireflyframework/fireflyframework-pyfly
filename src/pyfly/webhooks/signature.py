# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HMAC signature validation for inbound webhook requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class SignatureValidator(Protocol):
    def is_valid(self, *, body: bytes, signature: str | None) -> bool: ...


class NoOpSignatureValidator:
    def is_valid(self, *, body: bytes, signature: str | None) -> bool:
        return True


class HmacSignatureValidator:
    """Verifies a ``sha256=...`` style HMAC header against an expected secret."""

    def __init__(self, secret: str, *, header_prefix: str = "sha256=") -> None:
        self._secret = secret.encode("utf-8")
        self._prefix = header_prefix

    def is_valid(self, *, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        if signature.startswith(self._prefix):
            signature = signature[len(self._prefix) :]
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class StripeSignatureValidator:
    """Validates Stripe webhook signatures from the ``Stripe-Signature`` header.

    The header value is ``t=<timestamp>,v1=<hmac>[,v1=<hmac>...]``.  The signed
    payload is ``f"{timestamp}.{body}"`` (UTF-8).  Requests older than
    *tolerance_seconds* are rejected to prevent replay attacks.
    """

    def __init__(self, secret: str, *, tolerance_seconds: int = 300) -> None:
        self._secret = secret.encode("utf-8")
        self._tolerance = tolerance_seconds

    def is_valid(self, *, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        try:
            parts = dict(pair.split("=", 1) for pair in signature.split(",") if "=" in pair)
            timestamp_str = parts.get("t", "")
            if not timestamp_str:
                return False
            timestamp = int(timestamp_str)
        except (ValueError, AttributeError):
            return False

        if abs(time.time() - timestamp) > self._tolerance:
            return False

        v1_values = [v for k, v in (pair.split("=", 1) for pair in signature.split(",") if "=" in pair) if k == "v1"]
        if not v1_values:
            return False

        signed_payload = f"{timestamp}.{body.decode('utf-8', errors='replace')}".encode()
        expected = hmac.new(self._secret, signed_payload, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, v1) for v1 in v1_values)


class GitHubSignatureValidator:
    """Validates GitHub webhook signatures from the ``X-Hub-Signature-256`` header.

    GitHub sends ``sha256=<hmac>`` computed over the raw body using the webhook
    secret.  This is a named alias that wraps :class:`HmacSignatureValidator`
    with the ``sha256=`` prefix.
    """

    def __init__(self, secret: str) -> None:
        self._inner = HmacSignatureValidator(secret, header_prefix="sha256=")

    def is_valid(self, *, body: bytes, signature: str | None) -> bool:
        return self._inner.is_valid(body=body, signature=signature)


class TwilioSignatureValidator:
    """Validates Twilio request signatures from the ``X-Twilio-Signature`` header.

    .. important::
        **Not compatible with the body-based** :class:`SignatureValidator` **Protocol.**

        Twilio's signature scheme is ``base64(HMAC-SHA1(auth_token, url +
        sorted-concatenated-params))``.  It signs the *request URL* and *form
        parameters*, **not** the raw body, so the body-only
        ``is_valid(*, body, signature)`` Protocol cannot be used here.  If you
        use :class:`~pyfly.webhooks.processor.WebhookProcessor` you must verify
        Twilio requests outside the standard processor pipeline — for example in
        an HTTP middleware layer that has access to the full URL and parsed form
        data before the body is consumed as JSON.

        Use :meth:`is_valid` directly with the URL and decoded POST params.
    """

    def __init__(self, auth_token: str) -> None:
        self._token = auth_token.encode("utf-8")

    def is_valid(self, *, url: str, params: dict[str, str], signature: str | None) -> bool:
        """Return True if *signature* matches the Twilio HMAC for *url* + *params*.

        :param url: The full URL of the Twilio webhook endpoint (including scheme and host).
        :param params: The decoded form POST parameters from the request body.
        :param signature: The value of the ``X-Twilio-Signature`` header.
        """
        if not signature:
            return False
        # Build the signed string: URL + sorted key/value pairs concatenated.
        signed = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
        mac = hmac.new(self._token, signed.encode("utf-8"), hashlib.sha1).digest()
        expected = base64.b64encode(mac).decode("ascii")
        return hmac.compare_digest(expected, signature)
