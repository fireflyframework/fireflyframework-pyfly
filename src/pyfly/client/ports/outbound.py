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
"""Outbound port: HTTP client interface."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HttpClientPort(Protocol):
    """Abstract HTTP client interface."""

    async def request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class BoundedHttpClientPort(HttpClientPort, Protocol):
    """Optional bounded-response capability, separate from existing implementations."""

    async def request_bounded(self, method: str, url: str, *, max_response_bytes: int, **kwargs: Any) -> Any: ...


@runtime_checkable
class StreamingHttpResponsePort(Protocol):
    """Vendor-neutral response surface owned by a streaming request context.

    A consumed response exposes its retained content. An unconsumed asynchronous
    response exposes an async byte stream; callers must not close it independently
    of the request context when cancellation-protected cleanup is required.
    """

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def is_stream_consumed(self) -> bool: ...

    @property
    def content(self) -> bytes: ...

    @property
    def stream(self) -> AsyncIterable[bytes] | Iterable[bytes]: ...

    def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]: ...

    def aiter_raw(self, chunk_size: int | None = None) -> AsyncIterator[bytes]: ...

    async def aread(self) -> bytes: ...


@runtime_checkable
class StreamingHttpClientPort(HttpClientPort, Protocol):
    """Incremental responses with context-owned cleanup.

    Implementations honor explicit redirect/auth and credential-suppression
    options. ``sanitize_errors=True`` requests native ServiceTransportException
    errors without vendor request/response details; it never adds retries.
    """

    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> AbstractAsyncContextManager[StreamingHttpResponsePort]: ...
