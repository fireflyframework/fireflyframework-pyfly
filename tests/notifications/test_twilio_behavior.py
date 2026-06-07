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
"""Behavior tests for :class:`TwilioSmsProvider` with mocked HTTP I/O (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.notifications.models import EmailStatus, NotificationResult, SmsMessage
from pyfly.notifications.providers.twilio import TwilioSmsProvider


class FakeResponse:
    """Minimal stand-in for ``httpx.Response`` covering what the adapter touches."""

    def __init__(
        self,
        status_code: int,
        *,
        json_body: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"http {self.status_code}"
            raise AssertionError(msg)  # adapter never calls this on success; guard anyway


class FakeHttpClient:
    """Records outbound requests and returns a canned response per verb."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("post", url, kwargs))
        return self._response

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        return self._response

    async def put(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("put", url, kwargs))
        return self._response

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("delete", url, kwargs))
        return self._response

    async def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("patch", url, kwargs))
        return self._response


@pytest.mark.asyncio
async def test_send_builds_request_and_parses_sent_result() -> None:
    provider = TwilioSmsProvider("AC_sid_123", "tok_secret", from_number="+15550001111")
    fake = FakeHttpClient(FakeResponse(201, json_body={"sid": "SM_provider_abc"}))
    provider._http = fake  # inject before calling — _client() wraps it in PooledHttpClient

    message = SmsMessage(to="+15559876543", body="hello world")
    result = await provider.send(message)

    # (a) the outbound request the adapter built.
    assert len(fake.calls) == 1
    verb, url, kwargs = fake.calls[0]
    assert verb == "post"
    assert url == "https://api.twilio.com/2010-04-01/Accounts/AC_sid_123/Messages.json"
    assert kwargs["data"] == {
        "From": "+15550001111",
        "To": "+15559876543",
        "Body": "hello world",
    }
    assert kwargs["auth"] == ("AC_sid_123", "tok_secret")

    # (b) the adapter parsed the response into its domain return type.
    assert isinstance(result, NotificationResult)
    assert result.status == EmailStatus.SENT
    assert result.provider == "twilio"
    assert result.provider_id == "SM_provider_abc"
    assert result.id == message.id
    assert result.error is None


@pytest.mark.asyncio
async def test_send_prefers_message_sender_over_provider_from() -> None:
    provider = TwilioSmsProvider("AC_sid_123", "tok_secret", from_number="+15550001111")
    fake = FakeHttpClient(FakeResponse(201, json_body={"sid": "SM_xyz"}))
    provider._http = fake

    message = SmsMessage(to="+15559876543", body="hi", sender="+15552223333")
    await provider.send(message)

    _verb, _url, kwargs = fake.calls[0]
    # message.sender wins over the provider's configured from_number.
    assert kwargs["data"]["From"] == "+15552223333"


@pytest.mark.asyncio
async def test_send_maps_non_2xx_to_failed_result() -> None:
    provider = TwilioSmsProvider("AC_sid_123", "tok_secret", from_number="+15550001111")
    fake = FakeHttpClient(FakeResponse(401, text='{"code": 20003, "message": "Authenticate"}'))
    provider._http = fake

    message = SmsMessage(to="+15559876543", body="nope")
    result = await provider.send(message)

    assert result.status == EmailStatus.FAILED
    assert result.provider == "twilio"
    assert result.provider_id is None
    assert result.error is not None
    assert "http 401" in result.error
    assert "Authenticate" in result.error


@pytest.mark.asyncio
async def test_send_without_any_sender_raises() -> None:
    provider = TwilioSmsProvider("AC_sid_123", "tok_secret")  # no from_number
    fake = FakeHttpClient(FakeResponse(201, json_body={"sid": "SM_unused"}))
    provider._http = fake

    message = SmsMessage(to="+15559876543", body="orphan")  # no sender
    with pytest.raises(ValueError, match="needs a sender"):
        await provider.send(message)

    # nothing should have been sent over the wire.
    assert fake.calls == []
