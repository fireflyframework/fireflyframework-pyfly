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
from ssl import SSLContext
from typing import TYPE_CHECKING, Any

from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException

if TYPE_CHECKING:
    import httpx


class HttpxClientAdapter:
    """HTTPX adapter with optional bounded reads and explicit resource ownership.

    HTTPX remains a lazy import. Supplied clients and transports are borrowed
    unless ownership is explicitly transferred. Client-level settings belong
    on a supplied client; TLS/proxy/retry settings belong on a supplied transport.
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: timedelta = timedelta(seconds=30),
        headers: dict[str, str] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        owns_client: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        owns_transport: bool = False,
        verify: bool | SSLContext | None = None,
        proxy: str | httpx.Proxy | None = None,
        trust_env: bool | None = None,
        retries: int = 0,
    ) -> None:
        import httpx

        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer")
        if client is not None:
            if (
                base_url
                or timeout != timedelta(seconds=30)
                or headers is not None
                or transport is not None
                or owns_transport
                or verify is not None
                or proxy is not None
                or trust_env is not None
                or retries
            ):
                raise ValueError("Configure supplied client settings on the client itself")
            self._client = client
            self._owns_client = owns_client is True
            return
        if owns_client is False:
            raise ValueError("An internally constructed client must be owned by the adapter")
        if transport is None and owns_transport:
            raise ValueError("owns_transport requires a supplied transport")
        if transport is not None and (verify is not None or proxy is not None or retries):
            raise ValueError("Configure TLS, proxy and retries on the supplied transport")
        resolved_verify = True if verify is None else verify
        resolved_trust_env = True if trust_env is None else trust_env
        if retries:
            # Passing a transport disables HTTPX's environment proxy discovery.
            # Require an explicit choice instead of silently changing routing.
            if resolved_trust_env:
                raise ValueError("Explicit retries require trust_env=False; configure any proxy explicitly")
            transport = httpx.AsyncHTTPTransport(verify=resolved_verify, proxy=proxy, trust_env=False, retries=retries)
            owns_transport = True
            proxy = None
        if transport is not None and not owns_transport:
            borrowed = transport

            class BorrowedTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    return await borrowed.handle_async_request(request)

            transport = BorrowedTransport()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout.total_seconds(),
            headers=headers or {},
            transport=transport,
            verify=resolved_verify,
            proxy=proxy,
            trust_env=resolved_trust_env,
        )
        self._owns_client = True

    @staticmethod
    def _trace_headers(kwargs: dict[str, Any]) -> None:
        from pyfly.observability.propagation import inject_headers

        headers = dict(kwargs.get("headers") or {})
        inject_headers(headers)
        kwargs["headers"] = headers

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform the existing unbounded HTTPX request, including its auth semantics."""
        self._trace_headers(kwargs)
        return await self._client.request(method, url, **kwargs)

    async def request_bounded(self, method: str, url: str, *, max_response_bytes: int, **kwargs: Any) -> httpx.Response:
        """Read at most the configured identity-encoded payload before retaining it.

        Redirects and auth challenge flows are disabled. Event hooks and compressed
        responses are rejected. Caller-supplied clients/transports are trusted not
        to buffer internally. The cap excludes transport chunks and temporary copies.
        """
        import httpx

        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 0:
            raise ValueError("max_response_bytes must be a non-negative integer")
        if kwargs.pop("follow_redirects", False):
            raise ValueError("Bounded requests do not support automatic redirects")
        if kwargs.pop("auth", None) is not None:
            raise ValueError("Bounded requests require explicit auth headers, not auth flows")
        if any(self._client.event_hooks.values()):
            raise ValueError("Bounded requests do not support client event hooks")
        self._trace_headers(kwargs)
        headers = httpx.Headers(kwargs.pop("headers"))
        headers["Accept-Encoding"] = "identity"
        request = self._client.build_request(method, url, headers=headers, **kwargs)
        response = await self._client.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding != "identity":
                raise UnsupportedContentEncodingException("Bounded responses require identity content encoding")
            payload = bytearray()
            # A custom transport may return an already buffered response. Its prior
            # allocation is outside our control, but the returned payload is checked.
            if response.is_stream_consumed:
                if len(response.content) > max_response_bytes:
                    raise ResponseTooLargeException("Response exceeds max_response_bytes")
                payload.extend(response.content)
            else:
                if not isinstance(response.stream, httpx.AsyncByteStream):
                    raise TypeError("Bounded requests require an asynchronous response stream")
                # Iterate the public raw stream directly so HTTPX does not close
                # it outside our cancellation-protected cleanup on exhaustion.
                async for chunk in response.stream:
                    if len(chunk) > max_response_bytes - len(payload):
                        raise ResponseTooLargeException("Response exceeds max_response_bytes")
                    payload.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(payload),
                request=response.request,
                extensions=response.extensions,
            )
        finally:
            await self._close_response(response)

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        import asyncio

        from anyio import CancelScope

        # Shield AnyIO level cancellation and asyncio's direct Task.cancel().
        # Wait for cleanup even if cancellation arrives while close is suspended.
        with CancelScope(shield=True):
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # HTTPX also supports Trio through AnyIO.
                await response.aclose()
                return
            closing = asyncio.create_task(response.aclose())
            cancelled: asyncio.CancelledError | None = None
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            closing.result()
            if cancelled is not None:
                raise cancelled

    async def start(self) -> None:
        """No-op -- HTTPX clients are ready after construction."""

    async def stop(self) -> None:
        """Close owned resources; borrowed clients/transports remain available."""
        if self._owns_client:
            await self._client.aclose()
