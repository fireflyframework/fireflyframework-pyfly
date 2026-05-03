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
"""Password encoding port and bcrypt adapter."""

from __future__ import annotations

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
        return _bcrypt.checkpw(
            raw_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
