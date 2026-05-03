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
"""Session store protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    """Abstract session persistence interface.

    All session backends (in-memory, Redis, etc.) must implement this protocol.
    """

    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def exists(self, session_id: str) -> bool: ...
