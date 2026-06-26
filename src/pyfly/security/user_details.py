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
"""UserDetails / UserDetailsService — the credential-lookup SPI.

Spring Security parity: a :class:`UserDetailsService` resolves a username to a
:class:`UserDetails` (a stored password hash plus authorities), which the HTTP
Basic / form-login filters verify against a :class:`~pyfly.security.password.PasswordEncoder`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class UserDetails:
    """A resolved principal: a stored credential plus granted authorities."""

    username: str
    password_hash: str
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True


@runtime_checkable
class UserDetailsService(Protocol):
    """Port that resolves a username to its :class:`UserDetails`, or ``None``."""

    async def load_user_by_username(self, username: str) -> UserDetails | None: ...


class InMemoryUserDetailsService:
    """A :class:`UserDetailsService` backed by an in-memory dict (dev / testing)."""

    def __init__(self, *users: UserDetails) -> None:
        self._users: dict[str, UserDetails] = {u.username: u for u in users}

    async def load_user_by_username(self, username: str) -> UserDetails | None:
        return self._users.get(username)

    def add(self, user: UserDetails) -> None:
        self._users[user.username] = user
