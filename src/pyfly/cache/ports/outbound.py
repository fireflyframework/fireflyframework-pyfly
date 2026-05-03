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
"""Cache adapter protocol."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheAdapter(Protocol):
    """Abstract cache interface.

    All cache backends (Redis, in-memory, etc.) must implement this protocol.
    """

    async def get(self, key: str) -> Any | None: ...

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None: ...

    async def evict(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def clear(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
