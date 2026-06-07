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
"""Behavior tests for :class:`FirebasePushProvider` (FCM HTTP v1).

These exercise the real ``send`` code path with a fake httpx client injected at
``provider._http`` — no network, no Docker. We assert BOTH the outbound request
the adapter builds (URL, verb, payload, auth header) AND that the canned FCM
response is parsed into the correct :class:`NotificationResult`.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.notifications.models import EmailStatus, PushMessage
from pyfly.notifications.providers.firebase import FirebasePushProvider


class FakeResponse:
    """Minimal stand-in for ``httpx.Response`` covering what the adapter touches."""

    def __init__(self, *, status_code: int, json_body: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.headers: dict[str, str] = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class FakeHttpClient:
    """Records each outbound call and replays a queue of canned responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"verb": "POST", "url": url, **kwargs})
        return self._responses.pop(0)


def _provider(responses: list[FakeResponse]) -> FirebasePushProvider:
    provider = FirebasePushProvider(project_id="my-proj", access_token="ya29.token")
    provider._http = FakeHttpClient(responses)  # type: ignore[assignment]  # noqa: SLF001
    return provider


@pytest.mark.asyncio
async def test_send_success_builds_request_and_parses_message_name() -> None:
    fake = FakeHttpClient([FakeResponse(status_code=200, json_body={"name": "projects/my-proj/messages/0:abc"})])
    provider = FirebasePushProvider(project_id="my-proj", access_token="ya29.token")
    provider._http = fake  # type: ignore[assignment]  # noqa: SLF001

    msg = PushMessage(
        device_tokens=["device-token-1"],
        title="Hello",
        body="World",
        data={"badge": 3, "deep_link": "app://home"},
    )
    result = await provider.send(msg)

    # (a) outbound request the adapter built
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["verb"] == "POST"
    assert call["url"] == "https://fcm.googleapis.com/v1/projects/my-proj/messages:send"
    assert call["headers"] == {"Authorization": "Bearer ya29.token"}
    payload = call["json"]["message"]
    assert payload["token"] == "device-token-1"
    assert payload["notification"] == {"title": "Hello", "body": "World"}
    # data values are coerced to strings by the adapter
    assert payload["data"] == {"badge": "3", "deep_link": "app://home"}

    # (b) response parsed into the domain result
    assert result.id == msg.id
    assert result.provider == "firebase"
    assert result.status == EmailStatus.SENT
    assert result.provider_id == "projects/my-proj/messages/0:abc"
    assert result.error is None


@pytest.mark.asyncio
async def test_send_error_response_maps_to_failed_result() -> None:
    provider = _provider([FakeResponse(status_code=404, text="registration token not found")])

    result = await provider.send(PushMessage(device_tokens=["stale-token"], title="t", body="b"))

    assert result.status == EmailStatus.FAILED
    assert result.provider_id is None
    assert result.error == "stale-token: http 404"


@pytest.mark.asyncio
async def test_send_multi_token_partial_success_is_sent_with_error() -> None:
    fake = FakeHttpClient(
        [
            FakeResponse(status_code=200, json_body={"name": "projects/my-proj/messages/ok-1"}),
            FakeResponse(status_code=503, text="unavailable"),
        ]
    )
    provider = FirebasePushProvider(project_id="my-proj", access_token="ya29.token")
    provider._http = fake  # type: ignore[assignment]  # noqa: SLF001

    msg = PushMessage(device_tokens=["good", "bad"], title="t", body="b")
    result = await provider.send(msg)

    # one request per device token, in order
    assert [c["json"]["message"]["token"] for c in fake.calls] == ["good", "bad"]
    # partial success: at least one delivered => SENT, but failures recorded
    assert result.status == EmailStatus.SENT
    assert result.provider_id == "projects/my-proj/messages/ok-1"
    assert result.error == "bad: http 503"
