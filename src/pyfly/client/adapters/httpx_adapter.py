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
"""httpx-based HTTP client adapter."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


class HttpxClientAdapter:
    """HTTP client adapter backed by httpx.AsyncClient.

    Hexagonal: ``httpx`` is imported lazily in the constructor (not at module scope), so this
    module imports cleanly without the ``http`` extra — only the composition root that selects
    this adapter triggers the import.
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: timedelta = timedelta(seconds=30),
        headers: dict[str, str] | None = None,
    ) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout.total_seconds(),
            headers=headers or {},
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # Propagate the current trace context downstream (W3C traceparent etc.).
        from pyfly.observability.propagation import inject_headers

        headers = dict(kwargs.get("headers") or {})
        inject_headers(headers)
        kwargs["headers"] = headers
        return await self._client.request(method, url, **kwargs)

    async def start(self) -> None:
        """No-op -- httpx client is ready after construction."""

    async def stop(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
