# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for ResendEmailProvider with a mocked pooled HTTP client.

No network or Docker: a fake httpx client is injected at ``adapter._http`` so
``async with await adapter._client() as client:`` yields the fake, letting us
assert the outbound request the adapter builds and how it parses the response.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from pyfly.notifications.models import Attachment, EmailMessage, EmailStatus
from pyfly.notifications.providers.resend import ResendEmailProvider


class FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, status_code: int, json_body: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.headers: dict[str, str] = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"http {self.status_code}"
            raise RuntimeError(msg)


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
    fake = FakeHttpClient(FakeResponse(200, json_body={"id": "re_abc123"}))
    adapter = ResendEmailProvider(api_key="re_test_key")
    adapter._http = fake  # inject before calling — PooledHttpClient yields it

    msg = EmailMessage(
        to=["dest@example.com"],
        sender="from@example.com",
        subject="Hello",
        body_text="plain body",
    )
    result = await adapter.send(msg)

    # (a) outbound request the adapter built
    assert len(fake.calls) == 1
    verb, url, kwargs = fake.calls[0]
    assert verb == "post"
    assert url == "https://api.resend.com/emails"
    payload = kwargs["json"]
    assert payload["from"] == "from@example.com"
    assert payload["to"] == ["dest@example.com"]
    assert payload["subject"] == "Hello"
    assert payload["text"] == "plain body"
    assert "html" not in payload
    assert "cc" not in payload
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer re_test_key"
    assert headers["Content-Type"] == "application/json"

    # (b) response correctly parsed into the domain result
    assert result.status == EmailStatus.SENT
    assert result.provider == "resend"
    assert result.id == msg.id
    assert result.provider_id == "re_abc123"
    assert result.error is None


@pytest.mark.asyncio
async def test_send_includes_cc_bcc_html_and_base64_attachments() -> None:
    fake = FakeHttpClient(FakeResponse(202, json_body={"id": "re_xyz"}))
    adapter = ResendEmailProvider(api_key="re_key", default_from="default@x.io")
    adapter._http = fake

    raw = b"hello-bytes"
    msg = EmailMessage(
        to=["a@x.io"],
        cc=["c@x.io"],
        bcc=["b@x.io"],
        subject="rich",
        body_html="<p>hi</p>",
        attachments=[Attachment(filename="f.txt", content_type="text/plain", data=raw)],
    )
    result = await adapter.send(msg)

    payload = fake.calls[0][2]["json"]
    # default_from is used when message.sender is empty/falsy
    assert payload["from"] == "default@x.io"
    assert payload["cc"] == ["c@x.io"]
    assert payload["bcc"] == ["b@x.io"]
    assert payload["html"] == "<p>hi</p>"
    assert "text" not in payload
    assert payload["attachments"] == [{"filename": "f.txt", "content": base64.b64encode(raw).decode("ascii")}]
    # 202 is still 2xx -> SENT
    assert result.status == EmailStatus.SENT
    assert result.provider_id == "re_xyz"


@pytest.mark.asyncio
async def test_send_maps_non_2xx_to_failed_result() -> None:
    fake = FakeHttpClient(FakeResponse(422, text="invalid recipient"))
    adapter = ResendEmailProvider(api_key="re_key")
    adapter._http = fake

    msg = EmailMessage(to=["bad@x.io"], sender="from@x.io", subject="oops")
    result = await adapter.send(msg)

    # request was still attempted
    assert fake.calls[0][1] == "https://api.resend.com/emails"
    # error path: FAILED, no provider_id, error carries status + body
    assert result.status == EmailStatus.FAILED
    assert result.provider == "resend"
    assert result.id == msg.id
    assert result.provider_id is None
    assert result.error == "http 422: invalid recipient"
