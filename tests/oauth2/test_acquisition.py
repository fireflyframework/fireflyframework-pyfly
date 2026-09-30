"""Standalone acquisition never needs an application or web session."""

import asyncio
import json as json_module
from urllib.parse import parse_qs

import httpx
import pytest


def response(status, *, json=None, content=b"", headers=None):
    payload = json_module.dumps(json).encode() if json is not None else content
    return httpx.Response(status, stream=httpx.ByteStream(payload), headers=headers)


def api():
    from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints, generate_pkce, pkce_challenge

    return OAuth2Client, OAuth2ClientError, OAuth2Endpoints, generate_pkce, pkce_challenge


def test_pkce_vector_and_randomness():
    _, _, _, generate, challenge = api()
    assert challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    first, second = generate(), generate()
    assert first.verifier != second.verifier
    assert first.challenge == challenge(first.verifier)
    assert first.verifier not in repr(first)


async def test_code_exchange_public_client_and_redacted_result():
    client_type, _, endpoints_type, generate, _ = api()
    pkce = generate()

    def respond(request):
        data = parse_qs(request.content.decode())
        assert data["code_verifier"] == [pkce.verifier]
        assert data["grant_type"] == ["authorization_code"]
        assert "client_secret" not in data
        return response(
            200,
            json={
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    async with client_type(
        "cli", endpoints_type(token_endpoint="https://idp.test/token"), transport=httpx.MockTransport(respond)
    ) as client:
        result = await client.exchange_code(
            "secret-code", redirect_uri="http://127.0.0.1:1234/callback", code_verifier=pkce.verifier
        )
    assert result.access_token == "secret-access"
    assert "secret" not in repr(result)


class Clock:
    now = 0.0

    def __init__(self):
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("use_pkce", [False, True])
async def test_device_slow_down_is_cumulative(use_pkce):
    client_type, _, endpoints_type, _, _ = api()
    clock = Clock()
    responses = iter(
        [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"error": "slow_down"},
            {"access_token": "ok", "token_type": "Bearer"},
        ]
    )

    def respond(request):
        if request.url.path == "/device":
            return response(
                200,
                json={
                    "device_code": "secret-device",
                    "user_code": "ABCD",
                    "verification_uri": "https://idp.test/verify",
                    "expires_in": 90,
                },
            )
        value = next(responses)
        return response(400 if "error" in value else 200, json=value)

    async with client_type(
        "cli",
        endpoints_type(
            token_endpoint="https://idp.test/token", device_authorization_endpoint="https://idp.test/device"
        ),
        transport=httpx.MockTransport(respond),
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=use_pkce)
        assert "secret-device" not in repr(grant)
        token = await client.poll_device_token(grant)
    assert token.access_token == "ok"
    assert clock.waits == [5, 5, 10, 15]


@pytest.mark.parametrize("use_pkce", [False, True])
async def test_device_expiry_prevents_poll_and_cancellation_propagates(use_pkce):
    client_type, error_type, endpoints_type, _, _ = api()
    clock = Clock()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://idp.test/verify",
                "expires_in": 3,
            },
        )

    async with client_type(
        "cli",
        endpoints_type(
            token_endpoint="https://idp.test/token", device_authorization_endpoint="https://idp.test/device"
        ),
        transport=httpx.MockTransport(respond),
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=use_pkce)
        with pytest.raises(error_type, match="expired_token"):
            await client.poll_device_token(grant)
    assert calls == ["/device"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://idp.test/token",
        "https://user:secret@idp.test/token",
        "file:///tmp/token",
        "https://idp.test/token#fragment",
    ],
)
def test_unsafe_endpoint_rejected(endpoint):
    client_type, _, endpoints_type, _, _ = api()
    with pytest.raises(ValueError):
        client_type("cli", endpoints_type(token_endpoint=endpoint))


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (302, b"", {"location": "https://evil.test"}),
        (200, b"x" * 33, {}),
        (200, b"{}", {"content-encoding": "gzip"}),
        (200, b'{"id_token":"secret"}', {}),
        (400, b'{"error":"secret-sentinel","error_description":"secret"}', {}),
    ],
)
async def test_untrusted_response_fails_redacted(status, body, headers):
    client_type, error_type, endpoints_type, generate, _ = api()

    def respond(request):
        return response(status, content=body, headers=headers)

    async with client_type(
        "cli",
        endpoints_type(token_endpoint="https://idp.test/token"),
        transport=httpx.MockTransport(respond),
        max_response_bytes=32,
    ) as client:
        with pytest.raises(error_type) as error:
            await client.exchange_code(
                "secret-code", redirect_uri="http://127.0.0.1/callback", code_verifier=generate().verifier
            )
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("use_pkce", [False, True])
async def test_cancel_pending_device_sleep(use_pkce):
    client_type, _, endpoints_type, _, _ = api()
    entered = asyncio.Event()

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    def respond(request):
        assert request.url.path == "/device"
        return response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://idp.test/verify",
                "expires_in": 300,
            },
        )

    async with client_type(
        "cli",
        endpoints_type(
            token_endpoint="https://idp.test/token", device_authorization_endpoint="https://idp.test/device"
        ),
        transport=httpx.MockTransport(respond),
        sleep=sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=use_pkce)
        task = asyncio.create_task(client.poll_device_token(grant))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("outcome", ["access_denied", "expired_token", "invalid_grant"])
@pytest.mark.parametrize("use_pkce", [False, True])
async def test_device_terminal_errors_do_not_retry(outcome, use_pkce):
    client_type, error_type, endpoints_type, _, _ = api()
    clock = Clock()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/device":
            return response(
                200,
                json={"device_code": "d", "user_code": "u", "verification_uri": "https://idp.test/v", "expires_in": 50},
            )
        return response(400, json={"error": outcome})

    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token", "https://idp.test/device"),
        transport=httpx.MockTransport(respond),
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=use_pkce)
        with pytest.raises(error_type, match=outcome):
            await client.poll_device_token(grant)
    assert calls == ["/device", "/token"]


@pytest.mark.parametrize("use_pkce", [False, True])
async def test_device_timeout_backoff_and_late_response_expiry(use_pkce):
    client_type, error_type, endpoints_type, _, _ = api()
    clock = Clock()
    polls = 0

    def respond(request):
        nonlocal polls
        if request.url.path == "/device":
            return response(
                200,
                json={"device_code": "d", "user_code": "u", "verification_uri": "https://idp.test/v", "expires_in": 50},
            )
        polls += 1
        if polls == 1:
            raise httpx.ReadTimeout("secret-response")
        clock.now = 51
        return response(200, json={"access_token": "late", "token_type": "Bearer"})

    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token", "https://idp.test/device"),
        transport=httpx.MockTransport(respond),
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=use_pkce)
        with pytest.raises(error_type, match="expired_token"):
            await client.poll_device_token(grant)
    assert clock.waits == [5, 10]


async def test_response_cap_stops_stream_and_closes():
    client_type, error_type, endpoints_type, generate, _ = api()

    class Stream(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self):
            for _ in range(4):
                self.reads += 1
                yield b"1234"

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        max_response_bytes=6,
    ) as client:
        with pytest.raises(error_type):
            await client.exchange_code("code", redirect_uri="http://127.0.0.1/cb", code_verifier=generate().verifier)
    assert stream.reads == 2
    assert stream.closed


async def test_public_prebuffered_transport_response_is_supported():
    client_type, _, endpoints_type, generate, _ = api()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"access_token": "token", "token_type": "Bearer"})
        ),
    ) as client:
        token = await client.exchange_code(
            "code", redirect_uri="http://localhost/cb", code_verifier=generate().verifier
        )
    assert token.access_token == "token"


async def test_prebuffered_response_still_obeys_payload_limit():
    client_type, error_type, endpoints_type, generate, _ = api()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        max_response_bytes=10,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"access_token": "token", "token_type": "Bearer"})
        ),
    ) as client:
        with pytest.raises(error_type, match="invalid_response"):
            await client.exchange_code("code", redirect_uri="http://localhost/cb", code_verifier=generate().verifier)


@pytest.mark.parametrize("during_close", [False, True])
async def test_cancellation_releases_inflight_response(during_close):
    client_type, _, endpoints_type, generate, _ = api()
    entered = asyncio.Event()
    release = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            if not during_close:
                entered.set()
                await release.wait()
            yield b'{"access_token":"token","token_type":"Bearer"}'

        async def aclose(self):
            if during_close:
                entered.set()
                await release.wait()
            await asyncio.sleep(0)
            self.closed = True

    stream = Stream()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as client:
        task = asyncio.create_task(
            client.exchange_code("code", redirect_uri="http://localhost/cb", code_verifier=generate().verifier)
        )
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed


async def test_timeout_during_response_close_waits_for_cleanup():
    client_type, error_type, endpoints_type, generate, _ = api()

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"access_token":"token","token_type":"Bearer"}'

        async def aclose(self):
            await asyncio.sleep(0.03)
            self.closed = True

    stream = Stream()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        timeout=0.01,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as client:
        with pytest.raises(error_type, match="timeout"):
            await client.exchange_code("code", redirect_uri="http://localhost/cb", code_verifier=generate().verifier)
        assert stream.closed


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 1.5, True, 0, -1])
def test_response_cap_requires_positive_integer(cap):
    client_type, _, endpoints_type, _, _ = api()
    with pytest.raises(ValueError):
        client_type("cli", endpoints_type("https://idp.test/token"), max_response_bytes=cap)


async def test_huge_integer_expiry_is_a_redacted_invalid_response():
    client_type, error_type, endpoints_type, generate, _ = api()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        transport=httpx.MockTransport(
            lambda _: response(200, json={"access_token": "secret", "token_type": "Bearer", "expires_in": 10**400})
        ),
    ) as client:
        with pytest.raises(error_type, match="invalid_response"):
            await client.exchange_code("code", redirect_uri="http://localhost/cb", code_verifier=generate().verifier)


async def test_cancellation_during_client_close_waits_for_owned_transport():
    client_type, _, endpoints_type, _, _ = api()
    closing = asyncio.Event()
    release = asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request):
            return httpx.Response(200)

        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True

    transport = Transport()
    client = client_type("cli", endpoints_type("https://idp.test/token"), transport=transport)
    task = asyncio.create_task(client.aclose())
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed


async def test_deep_json_is_a_redacted_protocol_error():
    client_type, error_type, endpoints_type, generate, _ = api()
    body = b'{"nested":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}"
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    ) as client:
        with pytest.raises(error_type, match="invalid_response"):
            await client.exchange_code("code", redirect_uri="http://127.0.0.1/cb", code_verifier=generate().verifier)
