"""Selected native clients honor worker transport policy and stream cancellation ownership."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from pyfly.client import HttpClientPort, HttpxClientAdapter, StreamingHttpClientPort
from pyfly.client.auto_configuration import ClientAutoConfiguration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


async def test_native_di_client_policy_ignores_environment_proxy_and_preserves_host(monkeypatch):
    observed = []

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.headers.get("Host"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"native")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "client": {
                        "timeout": 2,
                        "trust-env": False,
                        "follow-redirects": False,
                        "transport-retries": 0,
                        "headers": {"Host": "admitted.worker"},
                    }
                }
            }
        ),
        auto_configurations=[ClientAutoConfiguration],
    )
    try:
        await context.start()
        port = context.get_bean(HttpClientPort)
        assert context.get_bean(StreamingHttpClientPort) is port
        async with port.stream(
            "GET", f"http://127.0.0.1:{server.server_port}", timeout={"connect": 1, "read": 1, "write": 1, "pool": 1}
        ) as response:
            assert b"".join([chunk async for chunk in response.aiter_bytes()]) == b"native"
        assert observed == ["admitted.worker"]
    finally:
        await context.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_native_stream_cancellation_closes_response_but_keeps_borrowed_client():
    closed = []
    entered = asyncio.Event()

    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            yield b"first"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Wire()))
    ) as client:
        port = HttpxClientAdapter(client=client)

        async def read():
            async with port.stream("GET", "http://native.invalid") as response:
                async for _ in response.aiter_bytes():
                    await asyncio.Event().wait()

        task = asyncio.create_task(read())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True] and not client.is_closed
        await port.stop()
        assert not client.is_closed


@pytest.mark.parametrize("mode", ["stream", "request_bounded", "request"])
async def test_suppressed_cookies_never_replay_a_previous_capability_response(mode):
    cookies = []

    def serve(request):
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"Set-Cookie": "session=capability-state; Path=/"}, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        port = HttpxClientAdapter(client=client)
        for _ in range(2):
            if mode == "stream":
                async with port.stream("POST", "http://native.invalid", suppress_cookies=True) as response:
                    await response.aread()
            elif mode == "request_bounded":
                await port.request_bounded(
                    "POST", "http://native.invalid", max_response_bytes=10, suppress_cookies=True
                )
            else:
                await port.request("POST", "http://native.invalid", suppress_cookies=True)
        assert cookies == [None, None]
