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
"""The ``http`` layer: a flagd document polled from another service's sync endpoint (spec 4.7).

The ETag advances only after a full document validates. Any failed request or rejected document raises so the
registry can retain the last accepted document and report the source as stale.
"""

from __future__ import annotations

from typing import Any

import httpx

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.sources import SourceSnapshot

__all__ = ["HttpFlagSource"]


class HttpFlagSource:
    """Poll a remote sync endpoint using conditional GETs."""

    name = "http"
    fail_fast = False

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        refresh_interval: float = 30.0,
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url:
            raise ValueError("pyfly.feature-flags.sources.http.url is required")
        self._url = url
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self.refresh_interval: float | None = refresh_interval
        self._client: httpx.AsyncClient | None = None
        self._etag: str | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        return self._client

    async def load(self) -> SourceSnapshot | None:
        headers = {"Accept": "application/json"}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = await self._http().get(self._url, headers=headers)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        try:
            raw: Any = response.json()
        except ValueError as error:
            raise ValueError(f"{self._url} did not answer a JSON document") from error
        document = parse_document(raw)
        self._etag = response.headers.get("etag")
        return SourceSnapshot(document, revision=self._etag)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
