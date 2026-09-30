"""Regression tests for bounded transport and explicit resource ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


@pytest.mark.asyncio
async def test_cap_stops_before_retaining_or_reading_remaining_chunks() -> None:
    from pyfly.client import ResponseTooLargeException

    stream = TrackingStream([b"1234", b"5678", b"unread"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(ResponseTooLargeException):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=6)
        assert stream.reads == 2
        assert stream.closed


@pytest.mark.asyncio
async def test_exact_limit_preserves_bytes_headers_status_and_closes() -> None:
    stream = TrackingStream([b"\xff", b"\x00abc"])

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(202, headers={"X-Test": "yes"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = HttpxClientAdapter(client=client)
        response = await adapter.request_bounded(
            "GET", "https://test.invalid", max_response_bytes=5, headers={"Authorization": "Bearer token"}
        )
        assert response.content == b"\xff\x00abc"
        assert response.status_code == 202
        assert response.headers["x-test"] == "yes"
        assert response.request.url == "https://test.invalid"
        assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadError("broken"), asyncio.CancelledError()])
async def test_error_and_cancellation_close_stream(error: BaseException) -> None:
    stream = TrackingStream([b"ok"], error)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(type(error)):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=10)
        assert stream.closed


@pytest.mark.asyncio
async def test_client_redirect_default_cannot_bypass_bounded_path() -> None:
    stream = TrackingStream([b"body", b"unread"])
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://other.invalid"}, stream=stream)

    from pyfly.client import ResponseTooLargeException

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(ResponseTooLargeException):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=2)
        assert calls == 1
        assert stream.reads == 1
        assert stream.closed
        with pytest.raises(ValueError, match="redirect"):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=2, follow_redirects=True)
        assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip", "unknown"])
async def test_compression_rejected_before_reading_or_decoding(encoding: str) -> None:
    from pyfly.client import UnsupportedContentEncodingException

    stream = TrackingStream([b"not compressed; must never be decoded"])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(UnsupportedContentEncodingException):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=8)
        assert stream.reads == 0
        assert stream.closed


@pytest.mark.asyncio
async def test_supplied_client_borrowed_by_default_and_explicitly_owned() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    adapter = HttpxClientAdapter(client=client)
    await adapter.stop()
    assert not client.is_closed
    owner = HttpxClientAdapter(client=client, owns_client=True)
    await owner.stop()
    await owner.stop()
    assert client.is_closed


@pytest.mark.asyncio
async def test_supplied_transport_borrowed_by_default() -> None:
    class Transport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=TrackingStream([b"ok"]))

        async def aclose(self) -> None:
            self.closed = True

    transport = Transport()
    adapter = HttpxClientAdapter(transport=transport, trust_env=False)
    assert (await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=2)).content == b"ok"
    await adapter.stop()
    assert not transport.closed
    owner = HttpxClientAdapter(transport=transport, owns_transport=True, trust_env=False)
    await owner.stop()
    assert transport.closed


@pytest.mark.asyncio
async def test_auth_challenge_flow_is_disabled_for_bounded_request() -> None:
    stream = TrackingStream([b"unread"])
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, headers={"www-authenticate": 'Digest realm="test", nonce="test"'}, stream=stream)

    from pyfly.client import ResponseTooLargeException

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), auth=httpx.DigestAuth("u", "p")) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(ResponseTooLargeException):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=0)
        assert calls == 1
        assert stream.closed


@pytest.mark.asyncio
async def test_response_hooks_rejected_before_network() -> None:
    calls = 0

    async def drain(response: httpx.Response) -> None:
        await response.aread()

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), event_hooks={"response": [drain]}) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(ValueError, match="event hooks"):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=1)
        assert calls == 0


@pytest.mark.asyncio
async def test_invalid_cap_and_auth_rejected_before_network() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid bounded request reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        adapter = HttpxClientAdapter(client=client)
        with pytest.raises(ValueError, match="max_response_bytes"):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=-1)
        with pytest.raises(ValueError, match="auth"):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=1, auth=("u", "p"))


@pytest.mark.asyncio
async def test_cancelling_inflight_request_waits_for_stream_cleanup() -> None:
    entered = asyncio.Event()

    class SlowStream(TrackingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    stream = SlowStream([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        task = asyncio.create_task(
            HttpxClientAdapter(client=client).request_bounded("GET", "https://test.invalid", max_response_bytes=3)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


@pytest.mark.asyncio
async def test_level_cancellation_shields_cleanup() -> None:
    import anyio

    entered = anyio.Event()

    class SlowStream(TrackingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await anyio.sleep_forever()
            yield b"unreachable"

    stream = SlowStream([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:

        async def read() -> None:
            await HttpxClientAdapter(client=client).request_bounded("GET", "https://test.invalid", max_response_bytes=3)

        async with anyio.create_task_group() as group:
            group.start_soon(read)
            await entered.wait()
            group.cancel_scope.cancel()
        assert stream.closed


@pytest.mark.asyncio
async def test_zero_cap_accepts_empty_response_and_content_length_cannot_bypass_cap() -> None:
    from pyfly.client import ResponseTooLargeException

    empty = TrackingStream([])
    lying = TrackingStream([b"too large"])
    responses = iter(
        [
            httpx.Response(204, stream=empty),
            httpx.Response(200, headers={"content-length": "0"}, stream=lying),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as client:
        adapter = HttpxClientAdapter(client=client)
        assert (await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=0)).content == b""
        with pytest.raises(ResponseTooLargeException):
            await adapter.request_bounded("GET", "https://test.invalid", max_response_bytes=0)
    assert empty.closed and lying.closed


@pytest.mark.asyncio
async def test_transport_errors_are_not_retried() -> None:
    calls = 0

    def fail(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable")

    adapter = HttpxClientAdapter(transport=httpx.MockTransport(fail), trust_env=False)
    try:
        with pytest.raises(httpx.ConnectError):
            await adapter.request_bounded("POST", "https://test.invalid", max_response_bytes=8, content=b"write")
        assert calls == 1
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_existing_request_keeps_auth_and_eager_response_semantics() -> None:
    stream = TrackingStream([b"one", b"two"])

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Basic dTpw"
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), auth=("u", "p")) as client:
        response = await HttpxClientAdapter(client=client).request("GET", "https://test.invalid")
        assert response.content == b"onetwo"
        assert stream.closed


@pytest.mark.asyncio
async def test_configuration_conflicts_fail_instead_of_silently_ignoring_policy() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="supplied client"):
            HttpxClientAdapter(client=client, trust_env=False)
        with pytest.raises(ValueError, match="supplied transport"):
            HttpxClientAdapter(transport=httpx.MockTransport(lambda _: httpx.Response(200)), verify=False)
        with pytest.raises(ValueError, match="owned"):
            HttpxClientAdapter(owns_client=False)
        with pytest.raises(ValueError, match="trust_env=False"):
            HttpxClientAdapter(retries=1)
        with pytest.raises(ValueError, match="non-negative"):
            HttpxClientAdapter(retries=-1)


def test_bounded_capability_does_not_change_existing_structural_port() -> None:
    from typing import Any

    from pyfly.client import BoundedHttpClientPort, HttpClientPort

    class ExistingClient:
        async def request(self, method: str, url: str, **kwargs: Any) -> Any:
            return None

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    assert isinstance(ExistingClient(), HttpClientPort)
    assert not isinstance(ExistingClient(), BoundedHttpClientPort)
    assert isinstance(
        HttpxClientAdapter(transport=httpx.MockTransport(lambda _: httpx.Response(200))), BoundedHttpClientPort
    )


@pytest.mark.asyncio
async def test_cancellation_during_close_still_releases_response() -> None:
    closing = asyncio.Event()
    release = asyncio.Event()

    class SlowClose(TrackingStream):
        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            self.closed = True

    stream = SlowClose([b"ok"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))) as client:
        task = asyncio.create_task(
            HttpxClientAdapter(client=client).request_bounded("GET", "https://test.invalid", max_response_bytes=2)
        )
        await closing.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


@pytest.mark.asyncio
async def test_explicit_tls_proxy_and_retry_controls_reach_owned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl
    from typing import Any

    factory = httpx.AsyncHTTPTransport
    observed: dict[str, Any] = {}

    def transport_factory(**kwargs: Any) -> httpx.AsyncHTTPTransport:
        observed.update(kwargs)
        return factory(**kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    context = ssl.create_default_context()
    adapter = HttpxClientAdapter(verify=context, proxy="http://127.0.0.1:9", trust_env=False, retries=2)
    try:
        assert observed["verify"] is context
        assert observed["proxy"] == "http://127.0.0.1:9"
        assert observed["trust_env"] is False
        assert observed["retries"] == 2
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_trust_env_false_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:0")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    adapter = HttpxClientAdapter(trust_env=False)
    try:
        response = await adapter.request_bounded("GET", f"http://127.0.0.1:{port}", max_response_bytes=2)
        assert response.content == b"ok"
    finally:
        await adapter.stop()
        server.close()
        await server.wait_closed()
