# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for SendGridEmailProvider with a mocked pooled HTTP client.

No network or Docker: a fake httpx client is injected at ``adapter._http`` so
``async with await adapter._client() as client:`` yields the fake, letting us
assert the outbound request the adapter builds and how it parses the response.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus
from pyfly.notifications.providers.sendgrid import SendGridEmailProvider


class FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(
        self,
        status_code: int,
        json_body: dict[str, Any] | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.headers: dict[str, str] = headers or {}

    def json(self) -> dict[str, Any]:
        return self._json


class FakeHttpClient:
    """Records outbound calls and returns a canned response."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("post", url, kwargs))
        return self._response

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        return self._response


@pytest.mark.asyncio
async def test_send_builds_request_and_parses_sent_result() -> None:
    """A 2xx response with X-Message-Id → SENT result with correct provider_id.

    Verifies URL, Authorization header, and the SendGrid payload shape.
    """
    fake = FakeHttpClient(
        FakeResponse(
            202,
            headers={"X-Message-Id": "sg_msg_abc123"},
        )
    )
    adapter = SendGridEmailProvider(api_key="SG.test_key")
    adapter._http = fake  # inject before calling — PooledHttpClient yields it

    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="Hello SendGrid",
        body_text="plain body",
        body_html="<p>html body</p>",
    )
    result = await adapter.send(msg)

    # (a) outbound request the adapter built
    assert len(fake.calls) == 1
    verb, url, kwargs = fake.calls[0]
    assert verb == "post"
    assert url == "https://api.sendgrid.com/v3/mail/send"

    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer SG.test_key"
    assert headers["Content-Type"] == "application/json"

    payload = kwargs["json"]
    # personalizations[0]: to + subject
    assert payload["personalizations"][0]["to"] == [{"email": "dest@example.com"}]
    assert payload["personalizations"][0]["subject"] == "Hello SendGrid"
    # from
    assert payload["from"]["email"] == "from@example.com"
    # content: both text and html
    content_types = {c["type"]: c["value"] for c in payload["content"]}
    assert content_types["text/plain"] == "plain body"
    assert content_types["text/html"] == "<p>html body</p>"
    # empty cc/bcc must be absent (SendGrid rejects null personalizations)
    assert "cc" not in payload["personalizations"][0]
    assert "bcc" not in payload["personalizations"][0]

    # (b) response correctly parsed into the domain result
    assert result.status == EmailStatus.SENT
    assert result.provider == "sendgrid"
    assert result.id == msg.id
    assert result.provider_id == "sg_msg_abc123"
    assert result.error is None


@pytest.mark.asyncio
async def test_send_includes_cc_bcc_and_base64_attachments() -> None:
    """CC/BCC recipients and attachments are encoded correctly."""
    raw = b"hello-bytes"
    fake = FakeHttpClient(
        FakeResponse(
            202,
            headers={"X-Message-Id": "sg_xyz"},
        )
    )
    adapter = SendGridEmailProvider(api_key="SG.key")
    adapter._http = fake

    msg = EmailMessage(
        to=["a@x.io"],
        cc=["c@x.io"],
        bcc=["b@x.io"],
        sender="s@x.io",
        subject="rich",
        body_html="<p>hi</p>",
        attachments=[Attachment(filename="f.txt", content_type="text/plain", data=raw)],
    )
    result = await adapter.send(msg)

    payload = fake.calls[0][2]["json"]
    # cc/bcc present when non-empty
    assert payload["personalizations"][0]["cc"] == [{"email": "c@x.io"}]
    assert payload["personalizations"][0]["bcc"] == [{"email": "b@x.io"}]
    # attachment is base64-encoded
    assert payload["attachments"] == [
        {
            "filename": "f.txt",
            "type": "text/plain",
            "content": base64.b64encode(raw).decode("ascii"),
        }
    ]
    assert result.status == EmailStatus.SENT
    assert result.provider_id == "sg_xyz"


@pytest.mark.asyncio
async def test_send_template_id_sets_dynamic_template_data() -> None:
    """When template_id is set, provider-native template routing is included in the payload."""
    fake = FakeHttpClient(
        FakeResponse(
            202,
            headers={"X-Message-Id": "sg_tmpl"},
        )
    )
    adapter = SendGridEmailProvider(api_key="SG.key")
    adapter._http = fake

    msg = EmailMessage(
        to=["u@x.io"],
        sender="s@x.io",
        subject="tmpl",
        template_id="d-abc123",
        template_data={"name": "Alice"},
    )
    result = await adapter.send(msg)

    payload = fake.calls[0][2]["json"]
    assert payload["template_id"] == "d-abc123"
    assert payload["personalizations"][0]["dynamic_template_data"] == {"name": "Alice"}
    assert result.status == EmailStatus.SENT


@pytest.mark.asyncio
async def test_send_maps_non_2xx_to_failed_result() -> None:
    """A non-2xx response → FAILED result with error message; no provider_id."""
    fake = FakeHttpClient(FakeResponse(400, text="bad request"))
    adapter = SendGridEmailProvider(api_key="SG.key")
    adapter._http = fake

    msg = EmailMessage(to=["bad@x.io"], sender="from@x.io", subject="oops")
    result = await adapter.send(msg)

    # request was still attempted
    assert fake.calls[0][1] == "https://api.sendgrid.com/v3/mail/send"
    # error path: FAILED, no provider_id, error carries status + body
    assert result.status == EmailStatus.FAILED
    assert result.provider == "sendgrid"
    assert result.id == msg.id
    assert result.provider_id is None
    assert "400" in (result.error or "")
    assert "bad request" in (result.error or "")
