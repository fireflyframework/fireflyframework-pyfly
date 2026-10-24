# Copyright 2026 Firefly Software Foundation
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

"""Encryption providers for sensitive data at rest.

This module provides encryption capabilities for protecting sensitive data
stored in memory backends, configuration files, and logs.

Features:
- AES-256-GCM encryption (authenticated encryption)
- Base64 encoding for safe storage
- Key derivation from passwords
- Memory store wrapper for transparent encryption

Example:
    Basic encryption::

        from pyfly.agentic.security.encryption import AESEncryptionProvider

        provider = AESEncryptionProvider(key="my-secret-key-32-bytes-long!!")

        # Encrypt
        encrypted = provider.encrypt("sensitive data")

        # Decrypt
        decrypted = provider.decrypt(encrypted)

    Encrypted memory store::

        from pyfly.agentic.security.encryption import EncryptedMemoryStore
        from pyfly.agentic.memory.store import InMemoryStore

        # Wrap any memory store with encryption
        base_store = InMemoryStore()
        encrypted_store = EncryptedMemoryStore(base_store, encryption_key="secret")

        # Use like a normal store - encryption is transparent
        encrypted_store.save("namespace", entry)
        loaded = encrypted_store.load("namespace")
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from pyfly.agentic._runtime_context import RuntimeDefault

if TYPE_CHECKING:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
else:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:  # pragma: no cover - optional dep
        hashes = None
        AESGCM = None
        PBKDF2HMAC = None

from pyfly.agentic.config import AgenticConfig, get_config
from pyfly.agentic.memory.types import MemoryEntry

logger = logging.getLogger(__name__)


@runtime_checkable
class EncryptionProvider(Protocol):
    """Protocol for encryption/decryption providers.

    Implementations must provide symmetric encryption with authentication
    (e.g., AES-GCM) to ensure both confidentiality and integrity.
    """

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext and return base64-encoded ciphertext.

        Args:
            plaintext: String to encrypt.

        Returns:
            Base64-encoded encrypted data (safe for storage).
        """
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt base64-encoded ciphertext and return plaintext.

        Args:
            ciphertext: Base64-encoded encrypted data.

        Returns:
            Decrypted plaintext string.

        Raises:
            ValueError: If decryption fails (wrong key, corrupted data).
        """
        ...


class AESEncryptionProvider:
    """AES-256-GCM encryption provider.

    Provides authenticated encryption using AES-256 in GCM mode, which ensures
    both confidentiality and integrity. Each encryption operation uses a
    random nonce to ensure uniqueness.

    Parameters:
        key: 32-byte encryption key (for AES-256). If a string is provided
            and it's not 32 bytes, it will be derived using PBKDF2.

    Example::

        # With explicit 32-byte key
        provider = AESEncryptionProvider(key=b"12345678901234567890123456789012")

        # With password (auto-derives 32-byte key)
        provider = AESEncryptionProvider(key="my-password")

        encrypted = provider.encrypt("sensitive data")
        decrypted = provider.decrypt(encrypted)

    Note:
        Requires the optional ``security`` dependency group:
        ``pip install pyfly[security]``
    """

    def __init__(self, key: str | bytes) -> None:
        if AESGCM is None or PBKDF2HMAC is None or hashes is None:
            raise ImportError("Encryption support requires 'cryptography'. Install with: pip install pyfly[security]")

        # Store raw key bytes for per-call salt derivation
        self._raw_key = key.encode("utf-8") if isinstance(key, str) else key
        self._AESGCM = AESGCM
        self._PBKDF2HMAC = PBKDF2HMAC
        self._hashes = hashes

        # If the key is exactly 32 bytes, use it directly (pre-derived)
        self._direct_key: bytes | None
        if len(self._raw_key) == 32:
            self._direct_key = self._raw_key
        else:
            self._direct_key = None

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive a 32-byte key from the raw key using PBKDF2 with the given salt."""
        if self._direct_key is not None:
            return self._direct_key
        kdf = self._PBKDF2HMAC(
            algorithm=self._hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
        )
        return kdf.derive(self._raw_key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext using AES-256-GCM.

        Args:
            plaintext: String to encrypt.

        Returns:
            Base64-encoded string containing: salt[16] + nonce[12] + ciphertext + tag
        """
        # Generate random 16-byte salt for PBKDF2 key derivation
        salt = os.urandom(16)
        key_bytes = self._derive_key(salt)
        cipher = self._AESGCM(key_bytes)

        # Generate random 12-byte nonce (recommended for GCM)
        nonce = os.urandom(12)

        # Encrypt (returns ciphertext + 16-byte authentication tag)
        plaintext_bytes = plaintext.encode("utf-8")
        ciphertext = cipher.encrypt(nonce, plaintext_bytes, None)

        # Combine salt + nonce + ciphertext for storage
        encrypted_data = salt + nonce + ciphertext

        # Base64 encode for safe storage
        return base64.b64encode(encrypted_data).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt AES-256-GCM ciphertext.

        Args:
            ciphertext: Base64-encoded encrypted data.

        Returns:
            Decrypted plaintext string.

        Raises:
            ValueError: If decryption fails (wrong key, corrupted data, tampered data).
        """
        try:
            # Base64 decode
            encrypted_data = base64.b64decode(ciphertext.encode("ascii"))

            # Split salt, nonce, and ciphertext
            salt = encrypted_data[:16]
            nonce = encrypted_data[16:28]
            ciphertext_bytes = encrypted_data[28:]

            # Derive key from salt
            key_bytes = self._derive_key(salt)
            cipher = self._AESGCM(key_bytes)

            # Decrypt (automatically verifies authentication tag)
            plaintext_bytes = cipher.decrypt(nonce, ciphertext_bytes, None)

            return plaintext_bytes.decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Decryption failed: {exc}") from exc


class EncryptedMemoryStore:
    """Transparent encryption wrapper for any MemoryStore.

    Wraps any memory store implementation and automatically encrypts/decrypts
    the content field of MemoryEntry objects. All other fields (metadata, keys,
    etc.) remain unencrypted for indexing and querying.

    Parameters:
        store: Underlying MemoryStore to wrap.
        encryption_key: Encryption key (32 bytes for AES-256, or password).
        provider: Optional custom EncryptionProvider. If None, uses AESEncryptionProvider.

    Example::

        from pyfly.agentic.memory.store import InMemoryStore
        from pyfly.agentic.security.encryption import EncryptedMemoryStore

        base_store = InMemoryStore()
        encrypted_store = EncryptedMemoryStore(
            base_store,
            encryption_key="my-secret-key"
        )

        # Use normally - encryption is transparent
        entry = MemoryEntry(key="api_key", content="sk-secret123")
        encrypted_store.save("namespace", entry)

        # Content is encrypted in base_store, but decrypted when loaded
        loaded = encrypted_store.load_by_key("namespace", "api_key")
        assert loaded.content == "sk-secret123"

    Note:
        Only the ``content`` field is encrypted. Metadata, keys, timestamps,
        and other fields remain plaintext for efficient querying.
    """

    _CODEC = "pyfly.agentic.memory.json.v1:"

    def __init__(
        self,
        store: Any,
        encryption_key: str | bytes | None = None,
        provider: EncryptionProvider | None = None,
        *,
        owns_store: bool = True,
    ) -> None:
        if provider is None and not encryption_key:
            raise ValueError("encryption_key or an EncryptionProvider is required")
        self._store = store
        self._provider = provider if provider is not None else AESEncryptionProvider(encryption_key)  # type: ignore[arg-type]
        self._owns_store = owns_store
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop | None:
        return cast(asyncio.AbstractEventLoop | None, getattr(self._store, "owner_loop", None))

    def _encrypt_entry(self, entry: MemoryEntry) -> MemoryEntry:
        encoded = entry.model_copy()
        if entry.content is not None:
            payload = self._CODEC + json.dumps(
                entry.content, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            encoded.content = self._provider.encrypt(payload)
        return encoded

    def _decrypt_entry(self, entry: MemoryEntry) -> MemoryEntry:
        if entry.content is None:
            return entry.model_copy()
        plaintext = self._provider.decrypt(str(entry.content))
        content = json.loads(plaintext[len(self._CODEC) :]) if plaintext.startswith(self._CODEC) else plaintext
        return entry.model_copy(update={"content": content})

    def save(self, namespace: str, entry: MemoryEntry) -> None:
        self._store.save(namespace, self._encrypt_entry(entry))

    def load(self, namespace: str) -> list[MemoryEntry]:
        return [self._decrypt_entry(entry) for entry in self._store.load(namespace)]

    def load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        entry = self._store.load_by_key(namespace, key)
        return None if entry is None else self._decrypt_entry(entry)

    def delete(self, namespace: str, entry_id: str) -> None:
        self._store.delete(namespace, entry_id)

    def clear(self, namespace: str) -> None:
        self._store.clear(namespace)

    async def async_save(self, namespace: str, entry: MemoryEntry) -> None:
        from pyfly.agentic.memory._async_store import call_store

        await call_store(self._store, "save", namespace, self._encrypt_entry(entry))

    async def async_load(self, namespace: str) -> list[MemoryEntry]:
        from pyfly.agentic.memory._async_store import call_store

        entries = await call_store(self._store, "load", namespace)
        return [self._decrypt_entry(entry) for entry in entries]

    async def async_load_by_key(self, namespace: str, key: str) -> MemoryEntry | None:
        from pyfly.agentic.memory._async_store import call_store

        entry = await call_store(self._store, "load_by_key", namespace, key)
        return None if entry is None else self._decrypt_entry(entry)

    async def async_delete(self, namespace: str, entry_id: str) -> None:
        from pyfly.agentic.memory._async_store import call_store

        await call_store(self._store, "delete", namespace, entry_id)

    async def async_clear(self, namespace: str) -> None:
        from pyfly.agentic.memory._async_store import call_store

        await call_store(self._store, "clear", namespace)

    async def async_replace(self, namespace: str, entry: MemoryEntry) -> None:
        from pyfly.agentic.memory._async_store import call_store

        encrypted = self._encrypt_entry(entry)
        replace = getattr(self._store, "async_replace", None)
        if replace is not None:
            await call_store(self._store, "replace", namespace, encrypted)
            return
        existing = await call_store(self._store, "load_by_key", namespace, entry.key)
        if existing is not None:
            await call_store(self._store, "delete", namespace, existing.entry_id)
        await call_store(self._store, "save", namespace, encrypted)

    async def initialize(self) -> None:
        initialize = getattr(self._store, "initialize", None)
        if initialize is not None:
            result = initialize()
            if inspect.isawaitable(result):
                await result
        self._closed = False

    def initialize_sync(self) -> None:
        initialize = getattr(self._store, "initialize_sync", None)
        if initialize is not None:
            initialize()
        self._closed = False

    def close_sync(self) -> None:
        if self._closed:
            return
        if self._owns_store:
            closer = getattr(self._store, "close_sync", None) or getattr(self._store, "close", None)
            if closer is not None:
                if inspect.iscoroutinefunction(closer):
                    raise RuntimeError("Use await encrypted_store.aclose() for this asynchronous backend")
                closer()
        self._closed = True

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self._owns_store:
                closer = getattr(self._store, "aclose", None) or getattr(self._store, "close", None)
                if closer is not None:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
            self._closed = True


def create_encryption_provider_from_config(config: AgenticConfig | None = None) -> EncryptionProvider | None:
    """Create an encryption provider from framework configuration.

    Returns:
        EncryptionProvider instance if encryption is enabled, None otherwise.
    """
    cfg = config if config is not None else get_config()

    if not cfg.encryption_enabled:
        return None
    if not cfg.encryption_key:
        raise ValueError("encryption_key or an injected EncryptionProvider is required when encryption is enabled")

    return AESEncryptionProvider(key=cfg.encryption_key)


# Resolve a selected application's key without retaining another application's provider.

default_encryption_provider = RuntimeDefault[EncryptionProvider | None](
    "encryption_provider", create_encryption_provider_from_config
)
