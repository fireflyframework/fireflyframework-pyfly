# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Official async Azure SDK over an inert transport, never an Azure service."""

from __future__ import annotations

import importlib
import importlib.util
import json
import time
from uuid import UUID

import pytest

sdk = pytest.importorskip("azure.communication.email.aio")
from azure.core.credentials import AccessToken  # noqa: E402
from azure.core.pipeline.transport import AsyncHttpTransport  # noqa: E402

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus  # noqa: E402

ENDPOINT = "https://owned-fixture.communication.azure.com"


class Credential:
    def __init__(self):
        self.closed = 0
        self.tokens = 0

    async def get_token(self, *scopes, **kwargs):
        self.tokens += 1
        return AccessToken("synthetic-private-token", int(time.time()) + 3600)

    async def close(self):
        self.closed += 1


class Response:
    def __init__(self, request, status, body, headers=None):
        self.request, self.status_code = request, status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.content = json.dumps(body).encode()
        self.reason, self.content_type, self.encoding = "fixture", "application/json", "utf-8"
        self.is_closed, self.is_stream_consumed = False, False
        self.url = request.url

    def json(self):
        return json.loads(self.content)

    def text(self, encoding=None):
        return self.content.decode(encoding or self.encoding)

    async def read(self):
        self.is_stream_consumed = True
        await self.close()
        return self.content

    async def iter_bytes(self, **kwargs):
        self.is_stream_consumed = True
        try:
            yield self.content
        finally:
            await self.close()

    async def close(self):
        self.is_closed = True


class Transport(AsyncHttpTransport):
    def __init__(self, *, post_status=202, terminal="Succeeded"):
        self.calls, self.responses = [], []
        self.closed = 0
        self.post_status, self.terminal = post_status, terminal

    async def open(self):
        pass

    async def close(self):
        self.closed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def send(self, request, **kwargs):
        self.calls.append((request.method, request.url, dict(request.headers), getattr(request, "content", None)))
        operation = request.headers.get("Operation-Id")
        if request.method == "POST":
            self.operation = operation
            response = Response(
                request,
                self.post_status,
                {
                    "id": operation,
                    "status": "Running",
                    "error": {"code": "fixture", "message": "synthetic-private-upstream"},
                },
                {
                    "Operation-Location": "https://untrusted.example.test/steal",
                    "WWW-Authenticate": 'Bearer error="insufficient_claims", claims="e30="',
                },
            )
        else:
            response = Response(request, 200, {"id": self.operation, "status": self.terminal})
        self.responses.append(response)
        return response


def native(monkeypatch, transport, credential=None, **kwargs):
    assert importlib.util.find_spec("pyfly.notifications.providers.azure_email") is not None, (
        "Native Azure email adapter is required"
    )
    module = importlib.import_module("pyfly.notifications.providers.azure_email")
    real = sdk.EmailClient
    monkeypatch.setattr(sdk, "EmailClient", lambda *args, **options: real(*args, transport=transport, **options))
    return module.AzureCommunicationEmailProvider(ENDPOINT, credential=credential or Credential(), **kwargs)


def message():
    return EmailMessage(
        id="stable-owned-message",
        sender="sender@example.test",
        to=["recipient@example.test"],
        body_html='<img src="cid:brand-logo">',
        attachments=[Attachment("logo.png", "image/png", b"fixture", content_id="brand-logo", inline=True)],
    )


async def test_real_sdk_maps_cid_stable_operation_and_bounded_same_endpoint_status(monkeypatch, caplog):
    transport, credential = Transport(), Credential()
    provider = native(monkeypatch, transport, credential)
    result = await provider.send(message())
    assert result.status == EmailStatus.SENT and result.provider_id == provider.operation_id(message().id)
    assert len([c for c in transport.calls if c[0] == "POST"]) == 1
    assert all(url.startswith(ENDPOINT + "/") for _, url, _, _ in transport.calls)
    sent = transport.calls[0]
    assert str(UUID(sent[2]["Operation-Id"])) == result.provider_id
    payload = json.loads(sent[3])
    assert payload["attachments"][0]["contentId"] == "brand-logo"
    assert payload["userEngagementTrackingDisabled"] is True
    assert payload["headers"]["Message-ID"]
    assert "synthetic-private" not in caplog.text and all(r.is_closed for r in transport.responses)
    await provider.stop()
    assert transport.closed == 1 and credential.closed == 0


async def test_pending_sdk_operation_times_out_unknown_without_post_replay(monkeypatch):
    transport = Transport(terminal="Running")
    provider = native(monkeypatch, transport, poll_timeout=0.025, poll_interval=0.005)
    try:
        result = await provider.send(message())
        assert result.status == EmailStatus.UNKNOWN and result.provider_id == provider.operation_id(message().id)
        assert len([c for c in transport.calls if c[0] == "POST"]) == 1
        assert len(transport.calls) < 12 and all(r.is_closed for r in transport.responses)
    finally:
        await provider.stop()


@pytest.mark.parametrize(
    "code,expected",
    [
        (400, EmailStatus.FAILED),
        (401, EmailStatus.FAILED),
        (429, EmailStatus.FAILED),
        (503, EmailStatus.UNKNOWN),
        (408, EmailStatus.UNKNOWN),
        (409, EmailStatus.UNKNOWN),
    ],
)
async def test_sdk_http_rejections_and_challenges_never_replay_or_expose_body(monkeypatch, code, expected):
    transport = Transport(post_status=code)
    provider = native(monkeypatch, transport)
    try:
        result = await provider.send(message())
        assert result.status == expected and "synthetic-private" not in (result.error or "")
        assert len(transport.calls) == 1, "No retry or credential challenge may replay this POST"
    finally:
        await provider.stop()


async def test_status_lookup_rejects_arbitrary_operation_urls_before_credentials(monkeypatch):
    transport, credential = Transport(), Credential()
    provider = native(monkeypatch, transport, credential)
    try:
        result = await provider.get_status("owned", "https://untrusted.example.test/operation")
        assert result.status == EmailStatus.FAILED and not transport.calls and not credential.tokens
    finally:
        await provider.stop()


async def test_owned_managed_identity_is_closed_when_client_construction_fails(monkeypatch):
    import azure.identity.aio

    assert importlib.util.find_spec("pyfly.notifications.providers.azure_email") is not None
    module = importlib.import_module("pyfly.notifications.providers.azure_email")
    credential = Credential()
    monkeypatch.setattr(azure.identity.aio, "ManagedIdentityCredential", lambda **kwargs: credential)

    def broken(*args, **kwargs):
        raise ValueError("synthetic-private-constructor")

    monkeypatch.setattr(sdk, "EmailClient", broken)
    provider = module.AzureCommunicationEmailProvider(ENDPOINT, managed_identity=True)
    with pytest.raises(RuntimeError, match="EMAIL_PROVIDER_CONFIGURATION_FAILED"):
        await provider.start()
    assert credential.closed == 1
    await provider.stop()
    assert credential.closed == 1


async def test_injected_client_and_credential_remain_caller_owned(monkeypatch):
    module = importlib.import_module("pyfly.notifications.providers.azure_email")
    credential, transport = Credential(), Transport()
    client = sdk.EmailClient(ENDPOINT, credential, transport=transport, retry_total=0)
    provider = module.AzureCommunicationEmailProvider(ENDPOINT, client=client)
    await provider.start()
    await provider.stop()
    assert not transport.closed and not credential.closed
    await client.close()
    assert transport.closed == 1
    await credential.close()


async def test_cancelling_polling_closes_current_response_without_replaying_post(monkeypatch):
    import asyncio

    entered = asyncio.Event()
    transport = Transport(terminal="Running")
    original = transport.send

    async def pause(request, **options):
        response = await original(request, **options)
        if request.method == "GET":

            async def chunks(**kwargs):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            response.iter_bytes = chunks
        return response

    transport.send = pause
    provider = native(monkeypatch, transport)
    task = asyncio.create_task(provider.send(message()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(response.is_closed for response in transport.responses)
    assert len([call for call in transport.calls if call[0] == "POST"]) == 1
    await provider.stop()


async def test_identity_wait_timeout_before_dispatch_is_failed_and_sends_no_post(monkeypatch):
    import asyncio

    class SlowCredential(Credential):
        async def get_token(self, *scopes, **kwargs):
            await asyncio.Event().wait()

    transport = Transport()
    provider = native(monkeypatch, transport, SlowCredential(), poll_timeout=0.025)
    try:
        result = await provider.send(message())
        assert result.status == EmailStatus.FAILED and not transport.calls
    finally:
        await provider.stop()


async def test_sdk_transport_error_after_consuming_post_is_unknown_and_never_replayed(monkeypatch):
    from azure.core.exceptions import ServiceRequestError

    class WrittenTransport(Transport):
        async def send(self, request, **kwargs):
            await super().send(request, **kwargs)
            assert request.method == "POST" and json.loads(request.content)["recipients"]
            raise ServiceRequestError("synthetic-private-transport-after-write")

    transport = WrittenTransport()
    provider = native(monkeypatch, transport)
    try:
        result = await provider.send(message())
        assert result.status == EmailStatus.UNKNOWN
        assert result.provider_id == provider.operation_id(message().id)
        assert result.error == "EMAIL_SEND_OUTCOME_UNKNOWN" and len(transport.calls) == 1
    finally:
        await provider.stop()
