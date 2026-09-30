"""Device PKCE stays bound to a single grant through the real HTTP client path."""

import asyncio
import base64
import hashlib
import traceback
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from tests.oauth2.test_acquisition import Clock, api, response


class DeviceProvider:
    """Protocol peer that binds a challenge to each device code and checks S256."""

    def __init__(self, *, supports_pkce=True, outcomes=("authorization_pending", None)):
        self.supports_pkce = supports_pkce
        self.outcomes = outcomes
        self.challenges = {}
        self.verifiers = {}
        self.device_forms = []
        self.token_forms = []

    def __call__(self, request):
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if request.url.path == "/device":
            self.device_forms.append(form)
            assert "code_verifier" not in form
            if self.supports_pkce:
                if form.get("code_challenge_method") != "S256" or not form.get("code_challenge"):
                    return response(400, json={"error": "invalid_request", "error_description": "Missing challenge"})
            elif "code_challenge" in form or "code_challenge_method" in form:
                return response(400, json={"error": "invalid_request"})
            code = f"device-{len(self.device_forms)}"
            self.challenges[code] = form.get("code_challenge")
            return response(
                200,
                json={
                    "device_code": code,
                    "user_code": "ABCD",
                    "verification_uri": "https://idp.test/verify",
                    "expires_in": 300,
                    "interval": 1,
                },
            )
        assert request.url.path == "/token"
        self.token_forms.append(form)
        code = form["device_code"]
        assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        assert "code_challenge" not in form
        assert "code_challenge_method" not in form
        if self.supports_pkce:
            verifier = form.get("code_verifier", "")
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if digest != self.challenges[code]:
                return response(400, json={"error": "invalid_grant"})
            if code in self.verifiers:
                assert verifier == self.verifiers[code]
            self.verifiers[code] = verifier
        else:
            assert "code_verifier" not in form
        poll = sum(item["device_code"] == code for item in self.token_forms)
        outcome = self.outcomes[min(poll - 1, len(self.outcomes) - 1)]
        if outcome == "timeout":
            raise httpx.ReadTimeout("provider request timeout")
        if outcome is not None:
            return response(400, json={"error": outcome})
        return response(200, json={"access_token": f"token-{code}", "token_type": "Bearer"})


def client_for(provider, *, clock=None):
    client_type, _, endpoints_type, _, _ = api()
    clock = clock or Clock()
    return client_type(
        "cli",
        endpoints_type("https://idp.test/token", "https://idp.test/device"),
        transport=httpx.MockTransport(provider),
        clock=clock,
        sleep=clock.sleep,
    )


async def test_device_pkce_pending_then_success_matches_challenge_without_revealing_verifier(caplog):
    caplog.set_level("DEBUG")
    provider = DeviceProvider()
    async with client_for(provider) as client:
        grant = await client.authorize_device(scopes=("openid", "api"), use_pkce=True)
        token = await client.poll_device_token(grant)
    assert token.access_token == "token-device-1"
    assert provider.device_forms[0]["scope"] == "openid api"
    assert provider.device_forms[0]["client_id"] == "cli"
    verifier = provider.verifiers["device-1"]
    assert len(provider.token_forms) == 2
    assert 43 <= len(verifier) <= 128
    assert verifier not in repr(grant)
    assert verifier not in repr(token)
    assert verifier not in caplog.text
    assert grant == replace(grant, _code_verifier="different-private-value")


async def test_two_device_pkce_grants_poll_in_reverse_order_without_crossing_verifiers():
    provider = DeviceProvider()
    async with client_for(provider) as client:
        first, second = await asyncio.gather(
            client.authorize_device(use_pkce=True), client.authorize_device(use_pkce=True)
        )
        second_token, first_token = await asyncio.gather(
            client.poll_device_token(second), client.poll_device_token(first)
        )
    assert first_token.access_token == f"token-{first.device_code}"
    assert second_token.access_token == f"token-{second.device_code}"
    assert len(set(provider.challenges.values())) == 2
    assert len(set(provider.verifiers.values())) == 2
    assert len(provider.token_forms) == 4


@pytest.mark.parametrize("explicit", [False, True])
async def test_default_device_flow_is_unchanged_for_provider_without_pkce(explicit):
    provider = DeviceProvider(supports_pkce=False)
    async with client_for(provider) as client:
        grant = await client.authorize_device(**({"use_pkce": False} if explicit else {}))
        token = await client.poll_device_token(grant)
    assert token.access_token == "token-device-1"
    assert provider.device_forms == [{"client_id": "cli"}]
    assert (
        provider.token_forms
        == [
            {
                "client_id": "cli",
                "device_code": "device-1",
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
        ]
        * 2
    )


async def test_pkce_rejection_does_not_fall_back_to_unprotected_device_authorization():
    _, error_type, _, _, _ = api()
    provider = DeviceProvider(supports_pkce=False)
    async with client_for(provider) as client:
        with pytest.raises(error_type, match="invalid_request"):
            await client.authorize_device(use_pkce=True)
    assert len(provider.device_forms) == 1
    assert provider.device_forms[0]["code_challenge_method"] == "S256"
    assert provider.token_forms == []


async def test_device_pkce_grant_wrong_owner_fails_before_sleep_or_http():
    provider = DeviceProvider()
    other_calls = []
    other_clock = Clock()
    async with (
        client_for(provider) as owner,
        client_for(lambda request: other_calls.append(request), clock=other_clock) as other,
    ):
        grant = await owner.authorize_device(use_pkce=True)
        with pytest.raises(ValueError, match="belongs to another"):
            await other.poll_device_token(grant)
    assert other_calls == []
    assert other_clock.waits == []
    assert provider.token_forms == []


@pytest.mark.parametrize("reflection", ["error", "description", "transport"])
async def test_device_pkce_reflected_verifier_is_redacted(reflection, caplog):
    caplog.set_level("DEBUG")
    _, error_type, _, _, _ = api()
    provider = DeviceProvider()
    captured = []

    def reflect(request):
        if request.url.path == "/device":
            return provider(request)
        verifier = parse_qs(request.content.decode())["code_verifier"][0]
        captured.append(verifier)
        if reflection == "transport":
            raise httpx.ConnectError(verifier, request=request)
        return response(
            400,
            json={
                "error": verifier if reflection == "error" else "invalid_grant",
                "error_description": verifier,
            },
        )

    async with client_for(reflect) as client:
        grant = await client.authorize_device(use_pkce=True)
        with pytest.raises(error_type) as caught:
            await client.poll_device_token(grant)
    assert len(captured) == 1
    displayed = str(caught.value) + repr(caught.value) + "".join(traceback.format_exception(caught.value)) + caplog.text
    assert captured[0] not in displayed


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", []])
async def test_device_pkce_requires_an_explicit_boolean_before_http(value):
    calls = []
    async with client_for(lambda request: calls.append(request)) as client:
        with pytest.raises(TypeError, match="use_pkce must be a bool"):
            await client.authorize_device(use_pkce=value)
    assert calls == []


def test_device_authorization_preserves_existing_positional_arguments():
    from pyfly.oauth2 import DeviceAuthorization

    owner = object()
    grant = DeviceAuthorization("device", "ABCD", "https://idp.test/v", 123, 5, "https://idp.test/v?code=ABCD", owner)
    assert grant._owner is owner
    assert grant.verification_uri_complete == "https://idp.test/v?code=ABCD"
    assert grant._code_verifier is None


async def test_device_pkce_keeps_verifier_through_pending_slowdown_and_timeout_backoff():
    provider = DeviceProvider(outcomes=("authorization_pending", "slow_down", "timeout", None))
    clock = Clock()
    async with client_for(provider, clock=clock) as client:
        grant = await client.authorize_device(use_pkce=True)
        token = await client.poll_device_token(grant)
    assert token.access_token == "token-device-1"
    assert clock.waits == [1, 1, 6, 12]
    assert len({form["code_verifier"] for form in provider.token_forms}) == 1
    assert len(provider.token_forms) == 4


@pytest.mark.parametrize("during_close", [False, True])
async def test_cancel_pkce_poll_closes_inflight_response_and_owned_transport(during_close):
    provider = DeviceProvider(outcomes=(None,))
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

    def respond(request):
        result = provider(request)
        if request.url.path == "/device":
            return result
        assert result.status_code == 200
        return httpx.Response(200, stream=stream)

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            await super().aclose()
            self.closed = True

    client_type, _, endpoints_type, _, _ = api()
    transport = Transport(respond)
    clock = Clock()
    async with client_type(
        "cli",
        endpoints_type("https://idp.test/token", "https://idp.test/device"),
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        grant = await client.authorize_device(use_pkce=True)
        task = asyncio.create_task(client.poll_device_token(grant))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    assert transport.closed
    assert len(provider.token_forms) == 1
