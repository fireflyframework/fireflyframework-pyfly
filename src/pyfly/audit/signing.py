# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Explicit callback-based Ed25519 checkpoint signing; no default keys or custody claims."""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Awaitable, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ConfigDict, TypeAdapter

from pyfly.audit.models import CheckpointSignature, Identifier
from pyfly.audit.ports import AuditError

PrivateKeyResolver = Callable[[str], Ed25519PrivateKey | Awaitable[Ed25519PrivateKey]]
PublicKeyResolver = Callable[[str], Ed25519PublicKey | None | Awaitable[Ed25519PublicKey | None]]


class Ed25519CheckpointSigner:
    def __init__(self, key_ref: str, resolve_key: PrivateKeyResolver) -> None:
        self._key_ref = TypeAdapter(Identifier, config=ConfigDict(hide_input_in_errors=True)).validate_python(key_ref)
        self._resolve_key = resolve_key

    def __repr__(self) -> str:
        return f"Ed25519CheckpointSigner(key_ref={self._key_ref!r})"

    async def sign(self, checkpoint_bytes: bytes) -> CheckpointSignature:
        if type(checkpoint_bytes) is not bytes or len(checkpoint_bytes) > 32768:
            raise ValueError("Checkpoint signing requires bounded immutable bytes")
        try:
            key = self._resolve_key(self._key_ref)
            if inspect.isawaitable(key):
                key = await key
            if not isinstance(key, Ed25519PrivateKey):
                raise TypeError
            signature = base64.urlsafe_b64encode(key.sign(checkpoint_bytes)).decode("ascii")
            return CheckpointSignature(key_ref=self._key_ref, algorithm="Ed25519", signature=signature)
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            raise AuditError("Checkpoint signing authority is unavailable") from None


class Ed25519CheckpointVerifier:
    def __init__(self, resolve_key: PublicKeyResolver) -> None:
        self._resolve_key = resolve_key

    async def verify(self, checkpoint_bytes: bytes, signature: CheckpointSignature) -> bool:
        try:
            signature = CheckpointSignature.model_validate(signature)
            if type(checkpoint_bytes) is not bytes or len(checkpoint_bytes) > 32768 or signature.algorithm != "Ed25519":
                return False
            raw = base64.b64decode(signature.signature, altchars=b"-_", validate=True)
            if len(raw) != 64 or base64.urlsafe_b64encode(raw).decode("ascii") != signature.signature:
                return False
            key = self._resolve_key(signature.key_ref)
            if inspect.isawaitable(key):
                key = await key
            if not isinstance(key, Ed25519PublicKey):
                return False
            key.verify(raw, checkpoint_bytes)
            return True
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            return False
