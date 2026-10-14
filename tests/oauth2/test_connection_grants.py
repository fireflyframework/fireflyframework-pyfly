"""Code and refresh acquisition bind a single explicit wire request, never an identity."""

import asyncio
import base64
from urllib.parse import parse_qs

import httpx
import pytest

from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints

ENDPOINTS = OAuth2Endpoints("https://identity.test/token")
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
TOKENS = {
    "access_token": "new-access",
    "refresh_token": "new-refresh",
    "token_type": "Bearer",
    "expires_in": 60,
    "scope": "repo.read",
    "id_token": "opaque-not-verified-identity",
}


async def call(client, operation, **kwargs):
    if operation == "code":
        return await client.exchange_code(
            "code", redirect_uri="https://forge.test/callback", code_verifier=VERIFIER, **kwargs
        )
    return await client.refresh_token("old-refresh", **kwargs)


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize("authentication", ["client_secret_basic", "client_secret_post"])
async def test_confidential_authentication_and_resource_have_exact_wire_location(operation, authentication):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=TOKENS)

    async with OAuth2Client(
        "client :+é", ENDPOINTS, client_secret="secret :+é", transport=httpx.MockTransport(respond)
    ) as client:
        result = await call(client, operation, authentication=authentication, resource="https://resource.test/mcp")
    assert len(requests) == 1 and requests[0].url == ENDPOINTS.token_endpoint
    expected = {"resource": ["https://resource.test/mcp"]}
    if operation == "code":
        expected.update(
            grant_type=["authorization_code"],
            code=["code"],
            redirect_uri=["https://forge.test/callback"],
            code_verifier=[VERIFIER],
        )
    else:
        expected.update(grant_type=["refresh_token"], refresh_token=["old-refresh"])
    if authentication == "client_secret_basic":
        assert base64.b64decode(requests[0].headers["authorization"].removeprefix("Basic ")) == (
            b"client+%3A%2B%C3%A9:secret+%3A%2B%C3%A9"
        )
    else:
        assert "authorization" not in requests[0].headers
        expected.update(client_id=["client :+é"], client_secret=["secret :+é"])
    assert parse_qs(requests[0].content.decode()) == expected
    assert result.access_token == "new-access" and result.refresh_token == "new-refresh"
    assert result.id_token == "opaque-not-verified-identity"
    assert "new-access" not in repr(result) and "new-refresh" not in repr(result)
    assert "opaque-not-verified" not in repr(result)


async def test_legacy_code_request_is_byte_identical_and_has_no_resource():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=TOKENS)

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        await call(client, "code")
    assert len(requests) == 1
    assert requests[0].content == (
        b"grant_type=authorization_code&code=code&redirect_uri=https%3A%2F%2Fforge.test%2Fcallback"
        b"&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk&client_id=client"
    )
    assert "authorization" not in requests[0].headers


async def test_public_refresh_default_does_not_invent_scopes_or_rotated_token():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "new-access", "token_type": "Bearer"})

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        token = await client.refresh_token("old-refresh")
    assert len(requests) == 1
    assert requests[0].content == b"grant_type=refresh_token&refresh_token=old-refresh&client_id=client"
    assert "authorization" not in requests[0].headers
    assert token.scope is None and token.refresh_token is None and token.id_token is None


async def test_refresh_sends_only_explicit_scopes_and_returns_provider_scope():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "access", "token_type": "Bearer", "scope": "repo.read"})

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        result = await client.refresh_token("refresh", scopes=("repo.read", "repo.metadata"), resource="urn:repo:42")
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh"],
        "client_id": ["client"],
        "scope": ["repo.read repo.metadata"],
        "resource": ["urn:repo:42"],
    }
    assert result.scope == "repo.read"


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize(
    "resource",
    [
        "https://api.test/mcp",
        "urn:example:resource",
        "api://application-id",
        "custom:registered-id",
        "https://[::1]/api?version=1",
        "api://app:1234/resource",
        "urn:example:a%5Bb%5D",
        "custom:opaque?x=a/b?c",
    ],
)
async def test_absolute_resources_are_identifiers_never_fetched(operation, resource):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=TOKENS)

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        await call(client, operation, resource=resource)
    assert len(requests) == 1 and requests[0].url == ENDPOINTS.token_endpoint
    assert parse_qs(requests[0].content.decode())["resource"] == [resource]


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize(
    "resource",
    [
        "",
        "relative/path",
        "//api.test/path",
        "urn:",
        "api://",
        "https:opaque",
        "urn:example:a[b]",
        "urn:example:a?query[bad]",
        "api://application:invalid",
        "https://api.test/a[b]",
        "https:///path",
        "https://user:private-sentinel@api.test/mcp",
        "urn:example:a#",
        "urn:example:a#fragment",
        "https://api.test/mcp\nprivate-sentinel",
        "https://api.test/\x7f",
        "urn:example:\x85",
        "urn:example:a b",
        "https://api.test:bad/mcp",
        "https://api.test:65536/mcp",
        "https://api.test/%zz",
        "urn:example:\ud800",
        "urn:example:<bad>",
        "https://api.test/back\\slash",
        "http://api.test/mcp",
        "http://127.0.0.1:1234/mcp",
        "urn:example:" + "x" * 2048,
        False,
        42,
    ],
)
async def test_invalid_resource_fails_before_http_without_reflection(operation, resource):
    def respond(request):
        pytest.fail("Invalid resource must not dispatch any request")

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError) as caught:
            await call(client, operation, resource=resource)
    assert "private-sentinel" not in str(caught.value)


async def test_resource_exact_byte_limit_and_explicit_literal_loopback():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=TOKENS)

    async with OAuth2Client(
        "client", ENDPOINTS, allow_loopback_http=True, transport=httpx.MockTransport(respond)
    ) as client:
        await client.refresh_token("refresh", resource="urn:example:" + "x" * 2036)
        await client.refresh_token("refresh", resource="http://127.0.0.1:1234/mcp")
        await client.refresh_token("refresh", resource="http://[::1]:1234/mcp")
        with pytest.raises(ValueError):
            await client.refresh_token("refresh", resource="http://localhost:1234/mcp")
    assert len(requests) == 3
    assert len(parse_qs(requests[0].content.decode())["resource"][0].encode()) == 2048


@pytest.mark.parametrize("opt_in", ["true", 1])
async def test_http_resource_requires_explicit_boolean_opt_in(opt_in):
    async with OAuth2Client(
        "client",
        ENDPOINTS,
        allow_loopback_http=opt_in,
        transport=httpx.MockTransport(lambda _: pytest.fail("No request expected")),
    ) as client:
        with pytest.raises(ValueError):
            await client.refresh_token("refresh", resource="http://127.0.0.1:1234/mcp")


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize(
    "client_id,secret,authentication",
    [
        (42, None, "client_secret_post"),
        ("client", 42, "client_secret_post"),
        ("client\nprivate-sentinel", "secret", "client_secret_post"),
        ("client", "secret\x85private-sentinel", "client_secret_post"),
        ("x" * 4097, None, "client_secret_post"),
        ("client", "é" * 2049, "client_secret_post"),
        ("client", "\ud800", "client_secret_post"),
        ("client", None, "client_secret_basic"),
        ("client", "", "client_secret_basic"),
        ("client", "secret", "none"),
        ("client", "secret", None),
        ("client", "secret", []),
        ("client", "secret", {}),
    ],
)
async def test_invalid_credentials_or_auth_method_fail_before_http(operation, client_id, secret, authentication):
    def respond(request):
        pytest.fail("Invalid credentials must not reach HTTP")

    async with OAuth2Client(
        client_id, ENDPOINTS, client_secret=secret, transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ValueError) as caught:
            await call(client, operation, authentication=authentication)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize("value", [None, False, 42, "", "x\nprivate-sentinel", "\ud800", "x" * 65537])
async def test_refresh_token_is_bounded_before_http(value):
    async with OAuth2Client(
        "client", ENDPOINTS, transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))
    ) as client:
        with pytest.raises(ValueError) as caught:
            await client.refresh_token(value)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize("value", [None, False, 42, "", "x\nprivate-sentinel", "\ud800", "x" * 4097])
async def test_code_is_bounded_before_http(value):
    async with OAuth2Client(
        "client", ENDPOINTS, transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))
    ) as client:
        with pytest.raises(ValueError):
            await client.exchange_code(value, redirect_uri="https://forge.test/cb", code_verifier=VERIFIER)


@pytest.mark.parametrize("scopes", ["read", ["read"], ("",), ("bad scope",), ("é",), ("x" * 257,), ("a",) * 65])
async def test_refresh_scope_shape_fails_before_http(scopes):
    async with OAuth2Client(
        "client", ENDPOINTS, transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))
    ) as client:
        with pytest.raises(ValueError):
            await client.refresh_token("refresh", scopes=scopes)


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize("outcome", ["redirect", "malformed", "compressed", "protocol", "transport", "timeout"])
async def test_failures_never_retry_or_expose_credentials(operation, outcome):
    requests = []

    def respond(request):
        requests.append(request)
        if outcome == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.test/private-sentinel"})
        if outcome == "protocol":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "private-sentinel"})
        if outcome == "transport":
            raise httpx.ConnectError("private-sentinel")
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-sentinel")
        if outcome == "compressed":
            return httpx.Response(
                200, stream=httpx.ByteStream(b"private-sentinel"), headers={"content-encoding": "gzip"}
            )
        return httpx.Response(200, content=b"private-sentinel")

    async with OAuth2Client("client", ENDPOINTS, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OAuth2ClientError) as caught:
            await call(client, operation, resource="https://resource.test/mcp")
    assert len(requests) == 1
    assert "private-sentinel" not in str(caught.value)
    assert caught.value.code == {"protocol": "invalid_grant", "transport": "transport_error", "timeout": "timeout"}.get(
        outcome, "invalid_response"
    )


@pytest.mark.parametrize("operation", ["code", "refresh"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_response_bounds_and_cancellation_close_without_replay(operation, cancel):
    entered = asyncio.Event()
    requests = []

    class Stream(httpx.AsyncByteStream):
        closed = False
        reads = 0

        async def __aiter__(self):
            self.reads += 1
            yield b"x" * 16
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            self.reads += 1
            yield b"x" * 17
            pytest.fail("Payload cap must stop before another chunk")

        async def aclose(self):
            self.closed = True

    stream = Stream()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    async with OAuth2Client(
        "client", ENDPOINTS, transport=httpx.MockTransport(respond), max_response_bytes=32
    ) as client:
        task = asyncio.create_task(call(client, operation, resource="https://resource.test/mcp"))
        if cancel:
            waiting = asyncio.create_task(entered.wait())
            await asyncio.wait({waiting, task}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                waiting.cancel()
                await task
            else:
                await waiting
                task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else OAuth2ClientError):
            await task
    assert len(requests) == 1 and stream.closed
    assert stream.reads == (1 if cancel else 2)


@pytest.mark.parametrize(
    "changes", [{"token_type": "MAC"}, {"expires_in": True}, {"refresh_token": 42}, {"scope": "bad  scope"}]
)
async def test_refresh_rejects_malformed_token_fields(changes):
    async with OAuth2Client(
        "client", ENDPOINTS, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={**TOKENS, **changes}))
    ) as client:
        with pytest.raises(OAuth2ClientError, match="^invalid_response$"):
            await client.refresh_token("refresh")
