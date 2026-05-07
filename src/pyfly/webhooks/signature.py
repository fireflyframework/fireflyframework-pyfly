# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HMAC signature validation for inbound webhook requests."""

from __future__ import annotations

import hashlib
import hmac
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
