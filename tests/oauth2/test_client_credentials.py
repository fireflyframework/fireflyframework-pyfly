"""Client credentials use a single explicit authentication method and bounded I/O."""

import asyncio
import base64
from urllib.parse import parse_qs

import httpx
import pytest

from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints

ENDPOINTS = OAuth2Endpoints("https://identity.test/token")
TOKEN = {"access_token": "secret-access+/==", "token_type": "bearer", "expires_in": 60}


@pytest.mark.parametrize("authentication", ["client_secret_basic", "client_secret_post"])
async def test_wire_authentication_and_scopes(authentication):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={**TOKEN, "scope": "api.read https://api.test/.default"})

    async with OAuth2Client(
        "client :+é", ENDPOINTS, client_secret="secret :+é", transport=httpx.MockTransport(respond)
    ) as client:
        result = await client.client_credentials(
            scopes=("api.read", "https://api.test/.default"), authentication=authentication
        )
    assert len(requests) == 1
    request = requests[0]
    assert request.url == ENDPOINTS.token_endpoint
    data = parse_qs(request.content.decode())
    assert data["grant_type"] == ["client_credentials"]
    assert data["scope"] == ["api.read https://api.test/.default"]
    if authentication == "client_secret_basic":
        assert set(data) == {"grant_type", "scope"}
        assert base64.b64decode(request.headers["authorization"].removeprefix("Basic ")) == (
            b"client+%3A%2B%C3%A9:secret+%3A%2B%C3%A9"
        )
    else:
        assert "authorization" not in request.headers
        assert data["client_id"] == ["client :+é"]
        assert data["client_secret"] == ["secret :+é"]
        assert len(data) == 4
    assert result.access_token == TOKEN["access_token"]
    assert result.token_type == "Bearer"
    assert result.expires_in == 60
    assert result.refresh_token is None and result.id_token is None
    assert "secret-access" not in repr(result)


async def test_default_basic_auth_omits_empty_scope():
    def respond(request):
        assert request.headers["authorization"] == "Basic Y2xpOnNlY3JldA=="
        assert request.content == b"grant_type=client_credentials"
        return httpx.Response(200, json=TOKEN)

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        assert (await client.client_credentials()).access_token == TOKEN["access_token"]


@pytest.mark.parametrize("secret", [None, "", 42, False])
async def test_missing_confidential_credentials_fail_before_http(secret):
    def respond(request):
        pytest.fail("Invalid credentials must never reach HTTP")

    async with OAuth2Client("cli", ENDPOINTS, client_secret=secret, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            await client.client_credentials()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scopes": "api"},
        {"scopes": ["api"]},
        {"scopes": ("",)},
        {"scopes": ("a b",)},
        {"scopes": ('a"b',)},
        {"scopes": ("a\\b",)},
        {"scopes": ("a\nb",)},
        {"scopes": ("é",)},
        {"scopes": (42,)},
        {"scopes": ("x" * 257,)},
        {"scopes": ("a",) * 65},
        {"scopes": ("x" * 256,) * 16},
        {"authentication": "none"},
        {"authentication": None},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
        {"timeout": "1"},
        {"timeout": 10**1000},
    ],
)
async def test_invalid_inputs_fail_before_http(kwargs):
    def respond(request):
        pytest.fail("Invalid inputs must never reach HTTP")

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((TypeError, ValueError)) as caught:
            await client.client_credentials(**kwargs)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("scopes", [("x" * 256,), ("a",) * 64, ("x" * 255,) * 16])
async def test_scope_boundaries_are_accepted(scopes):
    def respond(request):
        assert parse_qs(request.content.decode())["scope"] == [" ".join(scopes)]
        return httpx.Response(200, json=TOKEN)

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        await client.client_credentials(scopes=scopes)


@pytest.mark.parametrize(
    "changes",
    [
        {"token_type": "MAC"},
        {"access_token": ""},
        {"access_token": "bad token"},
        {"access_token": "\nsecret"},
        {"access_token": "é"},
        {"expires_in": -1},
        {"expires_in": True},
        {"refresh_token": "secret-refresh"},
        {"refresh_token": None},
        {"id_token": "secret-id"},
        {"id_token": None},
        {"scope": "a  b"},
        {"scope": "a\\b"},
        {"scope": "a " * 65},
        {"scope": "x" * 257},
    ],
)
async def test_invalid_token_responses_are_redacted(changes):
    def respond(request):
        return httpx.Response(200, json={**TOKEN, **changes})

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OAuth2ClientError, match="^invalid_response$"):
            await client.client_credentials()


@pytest.mark.parametrize("outcome", ["redirect", "protocol", "transport", "unknown", "malformed", "compressed"])
async def test_failures_are_redacted_without_retries(outcome):
    requests = []

    def respond(request):
        requests.append(request)
        if outcome == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.test/secret"})
        if outcome == "protocol":
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "secret"})
        if outcome == "transport":
            raise httpx.ConnectError("secret")
        if outcome == "unknown":
            return httpx.Response(400, json={"error": "secret"})
        if outcome == "compressed":
            return httpx.Response(200, stream=httpx.ByteStream(b"secret"), headers={"Content-Encoding": "gzip"})
        return httpx.Response(200, content=b"secret")

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OAuth2ClientError) as caught:
            await client.client_credentials()
    assert len(requests) == 1
    assert "secret" not in str(caught.value)
    assert caught.value.code == {"protocol": "invalid_client", "transport": "transport_error"}.get(
        outcome, "invalid_response"
    )


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, delay=False):
        self.delay = delay
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield b"x" * 16
        if self.delay:
            await asyncio.Event().wait()
        self.reads += 1
        yield b"x" * 17
        pytest.fail("Oversized stream must stop before the next chunk")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("delay", [False, True])
@pytest.mark.parametrize("constructor_timeout,call_timeout", [(1, 0.01), (0.01, 1)])
async def test_stream_limits_timeout_clamping_and_cleanup(delay, constructor_timeout, call_timeout):
    stream = Stream(delay=delay)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    async with OAuth2Client(
        "cli",
        ENDPOINTS,
        client_secret="secret",
        transport=httpx.MockTransport(respond),
        max_response_bytes=32,
        timeout=constructor_timeout,
    ) as client:
        with pytest.raises(OAuth2ClientError, match="^timeout$" if delay else "^invalid_response$"):
            await asyncio.wait_for(client.client_credentials(timeout=call_timeout), 0.5)
    assert stream.closed
    assert len(calls) == 1
    assert stream.reads == (1 if delay else 2)


@pytest.mark.parametrize(
    "client_id,secret",
    [
        (42, "secret"),
        ("cli\n", "secret"),
        ("cli", "secret\x00"),
        ("cli", "x" * 4097),
        ("x" * 4097, "secret"),
        ("cli", "\ud800"),
        ("cli", "secret\x7f"),
    ],
)
async def test_malformed_credentials_fail_before_http(client_id, secret):
    def respond(request):
        pytest.fail("Malformed credentials must never reach HTTP")

    async with OAuth2Client(
        client_id, ENDPOINTS, client_secret=secret, transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ValueError) as caught:
            await client.client_credentials()
    assert "secret" not in str(caught.value)


async def test_cancellation_propagates_and_closes_response_without_retry():
    started = asyncio.Event()
    closed = asyncio.Event()
    requests = []

    class PendingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=PendingStream())

    async with OAuth2Client("cli", ENDPOINTS, client_secret="secret", transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(client.client_credentials())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    assert len(requests) == 1


async def test_buffered_response_limit():
    async with OAuth2Client(
        "cli",
        ENDPOINTS,
        client_secret="secret",
        max_response_bytes=32,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 33)),
    ) as client:
        with pytest.raises(OAuth2ClientError, match="^invalid_response$"):
            await client.client_credentials()


@pytest.mark.parametrize("delay", [False, True])
async def test_default_transport_ignores_proxy_environment_and_bounds_delayed_http(monkeypatch, delay):
    requests = []
    finished = asyncio.Event()
    release = asyncio.Event()

    async def serve(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            requests.append(headers)
            if delay:
                await release.wait()
            else:
                body = b'{"access_token":"machine-token","token_type":"Bearer"}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    async with server:
        try:
            async with OAuth2Client(
                "cli",
                OAuth2Endpoints(f"http://127.0.0.1:{port}/token"),
                client_secret="secret",
                allow_loopback_http=True,
            ) as client:
                if delay:
                    with pytest.raises(OAuth2ClientError, match="^timeout$"):
                        await asyncio.wait_for(client.client_credentials(timeout=0.05), 1)
                else:
                    assert (await client.client_credentials()).access_token == "machine-token"
        finally:
            release.set()
            if requests:
                await asyncio.wait_for(finished.wait(), 1)
    assert len(requests) == 1
    assert b"Authorization: Basic Y2xpOnNlY3JldA==" in requests[0]


@pytest.mark.parametrize("operation", ["code", "device"])
async def test_other_operations_keep_form_authentication(operation):
    def respond(request):
        data = parse_qs(request.content.decode())
        assert data["client_id"] == ["cli"]
        assert data["client_secret"] == ["secret"]
        assert "authorization" not in request.headers
        if operation == "code":
            assert data["grant_type"] == ["authorization_code"]
            return httpx.Response(200, json={**TOKEN, "refresh_token": "refresh", "id_token": "id"})
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://identity.test/v",
                "expires_in": 60,
            },
        )

    async with OAuth2Client(
        "cli",
        OAuth2Endpoints("https://identity.test/token", "https://identity.test/device"),
        client_secret="secret",
        transport=httpx.MockTransport(respond),
    ) as client:
        if operation == "code":
            tokens = await client.exchange_code("code", redirect_uri="https://app.test/cb", code_verifier="x" * 43)
            assert tokens.refresh_token == "refresh" and tokens.id_token == "id"
        else:
            assert (await client.authorize_device()).device_code == "device"
