# Copyright 2026 Firefly Software Foundation.
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
"""Password encoding port and adapters.

Provides a :class:`PasswordEncoder` port and several adapters — bcrypt, PBKDF2,
scrypt, Argon2 — plus a :class:`DelegatingPasswordEncoder` that prefixes hashes
with a ``{id}`` so the algorithm can be migrated over time (Spring Security
parity with ``DelegatingPasswordEncoder`` / ``PasswordEncoderFactories``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Protocol, runtime_checkable

import bcrypt as _bcrypt


@runtime_checkable
class PasswordEncoder(Protocol):
    """Port for password hashing and verification."""

    def hash(self, raw_password: str) -> str:
        """Hash a raw password. Returns the hashed string."""
        ...

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        """Verify a raw password against a hashed password."""
        ...


class BcryptPasswordEncoder:
    """PasswordEncoder adapter using bcrypt.

    Args:
        rounds: Number of bcrypt hashing rounds (default: 12).
    """

    def __init__(self, rounds: int = 12) -> None:
        self._rounds = rounds

    def hash(self, raw_password: str) -> str:
        """Hash a raw password using bcrypt."""
        salt = _bcrypt.gensalt(rounds=self._rounds)
        return _bcrypt.hashpw(raw_password.encode("utf-8"), salt).decode("utf-8")

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        """Verify a raw password against a bcrypt hash."""
        try:
            return _bcrypt.checkpw(
                raw_password.encode("utf-8"),
                hashed_password.encode("utf-8"),
            )
        except (ValueError, TypeError):
            # Malformed / non-bcrypt stored value — treat as a non-match.
            return False


class Pbkdf2PasswordEncoder:
    """PasswordEncoder using PBKDF2-HMAC (stdlib ``hashlib``).

    Produces a self-describing string ``<algorithm>$<iterations>$<salt_b64>$<hash_b64>``.
    PBKDF2 is FIPS-friendly; defaults to 600k SHA-256 iterations (OWASP 2023).
    """

    def __init__(self, *, iterations: int = 600_000, salt_bytes: int = 16, algorithm: str = "sha256") -> None:
        self._iterations = iterations
        self._salt_bytes = salt_bytes
        self._algorithm = algorithm

    def hash(self, raw_password: str) -> str:
        salt = secrets.token_bytes(self._salt_bytes)
        digest = hashlib.pbkdf2_hmac(self._algorithm, raw_password.encode("utf-8"), salt, self._iterations)
        return (
            f"{self._algorithm}${self._iterations}$"
            f"{base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"
        )

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        try:
            algorithm, iterations_s, salt_b64, digest_b64 = hashed_password.split("$")
            iterations = int(iterations_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac(algorithm, raw_password.encode("utf-8"), salt, iterations, dklen=len(expected))
        return hmac.compare_digest(actual, expected)


class ScryptPasswordEncoder:
    """PasswordEncoder using scrypt (stdlib ``hashlib.scrypt``).

    Produces ``<n>$<r>$<p>$<salt_b64>$<hash_b64>``. Memory-hard; defaults follow
    common interactive-login parameters (n=2**14, r=8, p=1).
    """

    def __init__(self, *, n: int = 2**14, r: int = 8, p: int = 1, salt_bytes: int = 16, dklen: int = 32) -> None:
        self._n = n
        self._r = r
        self._p = p
        self._salt_bytes = salt_bytes
        self._dklen = dklen

    def hash(self, raw_password: str) -> str:
        salt = secrets.token_bytes(self._salt_bytes)
        digest = hashlib.scrypt(
            raw_password.encode("utf-8"), salt=salt, n=self._n, r=self._r, p=self._p, dklen=self._dklen
        )
        return (
            f"{self._n}${self._r}${self._p}$"
            f"{base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"
        )

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        try:
            n_s, r_s, p_s, salt_b64, digest_b64 = hashed_password.split("$")
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        try:
            actual = hashlib.scrypt(raw_password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        except ValueError:
            return False
        return hmac.compare_digest(actual, expected)


class Argon2PasswordEncoder:
    """PasswordEncoder using Argon2id (OWASP-preferred). Requires ``argon2-cffi``.

    The dependency is imported lazily so the rest of the security module works
    without it; install with ``pip install pyfly[argon2]`` to use this encoder.
    """

    def __init__(self, *, time_cost: int = 3, memory_cost: int = 65536, parallelism: int = 4) -> None:
        self._time_cost = time_cost
        self._memory_cost = memory_cost
        self._parallelism = parallelism

    def _hasher(self) -> object:
        try:
            from argon2 import PasswordHasher  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # pragma: no cover - exercised only without argon2-cffi
            raise ImportError("Argon2PasswordEncoder requires argon2-cffi — `pip install pyfly[argon2]`") from exc
        return PasswordHasher(time_cost=self._time_cost, memory_cost=self._memory_cost, parallelism=self._parallelism)

    def hash(self, raw_password: str) -> str:
        return str(self._hasher().hash(raw_password))  # type: ignore[attr-defined]

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        from argon2.exceptions import (  # type: ignore[import-not-found, unused-ignore]
            VerificationError,
            VerifyMismatchError,
        )

        try:
            return bool(self._hasher().verify(hashed_password, raw_password))  # type: ignore[attr-defined]
        except (VerifyMismatchError, VerificationError):
            return False


class DelegatingPasswordEncoder:
    """Password encoder that prefixes hashes with ``{id}`` and delegates by id.

    Spring Security parity (``DelegatingPasswordEncoder``): :meth:`hash` produces
    ``{<encoding_id>}<inner-hash>`` using the default encoder; :meth:`verify`
    reads the ``{id}`` prefix and dispatches to the matching encoder. A stored
    value with an unknown or missing prefix never matches. :meth:`upgrade_encoding`
    reports whether a stored hash should be re-hashed with the current default —
    enabling transparent on-login migration between algorithms.
    """

    def __init__(self, encoders: dict[str, PasswordEncoder], encoding_id: str) -> None:
        if encoding_id not in encoders:
            raise ValueError(f"encoding_id {encoding_id!r} is not present in the encoders map")
        self._encoders = dict(encoders)
        self._encoding_id = encoding_id

    @staticmethod
    def _split(stored: str) -> tuple[str | None, str]:
        """Return ``(id, remainder)`` for a ``{id}...`` value, or ``(None, stored)``."""
        if stored.startswith("{"):
            end = stored.find("}")
            if end > 0:
                return stored[1:end], stored[end + 1 :]
        return None, stored

    def hash(self, raw_password: str) -> str:
        inner = self._encoders[self._encoding_id].hash(raw_password)
        return f"{{{self._encoding_id}}}{inner}"

    def verify(self, raw_password: str, hashed_password: str) -> bool:
        encoding_id, inner = self._split(hashed_password)
        encoder = self._encoders.get(encoding_id) if encoding_id is not None else None
        if encoder is None:
            return False
        return encoder.verify(raw_password, inner)

    def upgrade_encoding(self, hashed_password: str) -> bool:
        """Whether *hashed_password* should be re-hashed with the current default."""
        encoding_id, _ = self._split(hashed_password)
        return encoding_id != self._encoding_id


def create_delegating_password_encoder(*, bcrypt_rounds: int = 12) -> DelegatingPasswordEncoder:
    """Build a :class:`DelegatingPasswordEncoder` with bcrypt as the default id.

    Mirrors Spring's ``PasswordEncoderFactories.createDelegatingPasswordEncoder()``:
    new hashes use bcrypt (``{bcrypt}``), while ``{pbkdf2}``, ``{scrypt}`` and
    ``{argon2}`` hashes are still recognised for verification and migration.
    """
    return DelegatingPasswordEncoder(
        {
            "bcrypt": BcryptPasswordEncoder(rounds=bcrypt_rounds),
            "pbkdf2": Pbkdf2PasswordEncoder(),
            "scrypt": ScryptPasswordEncoder(),
            "argon2": Argon2PasswordEncoder(),
        },
        encoding_id="bcrypt",
    )
