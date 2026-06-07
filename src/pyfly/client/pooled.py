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
"""Pooled HTTP client helper for outbound provider adapters.

Lets a provider keep ONE long-lived ``httpx.AsyncClient`` (connection pool reused across calls)
while leaving the existing ``async with await self._client() as client:`` call sites unchanged —
:class:`PooledHttpClient` is an async context manager that yields the shared client but does
**not** close it on exit. The client is closed once, on the provider's ``stop()`` lifecycle.
"""

from __future__ import annotations

from typing import Any


class PooledHttpClient:
    """Async-CM wrapper yielding a shared client without closing it on ``__aexit__``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: Any) -> bool:
        return False  # keep the pooled client open for reuse
