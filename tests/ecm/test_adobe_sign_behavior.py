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
"""Behavior tests for :class:`AdobeSignESignatureAdapter`.

These exercise the adapter against a fake httpx client injected at ``adapter._http``
so we verify BOTH the outbound request the adapter builds (URL, verb, payload, auth
headers) AND how it parses each response into its domain types — with no network,
Docker, or real httpx connections involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.ecm.adapters.adobe_sign import AdobeSignESignatureAdapter
from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    Recipient,
    SignatureRequest,
)

API_BASE = "https://api.eu1.adobesign.com/api/rest/v6"
TOKEN = "secret-integration-key"  # noqa: S105 - test fixture, not a real credential


class _HttpStatusError(Exception):
    """Mirrors ``httpx.HTTPStatusError`` for the error-path assertion."""


class FakeResponse:
    """A minimal stand-in for ``httpx.Response``."""

    def __init__(
        self,
        *,
        status_code: int,
        json_body: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise _HttpStatusError(msg)


class FakeHttpClient:
    """Records each outbound call and replays canned responses.

    ``responses`` maps the lower-case HTTP verb to the :class:`FakeResponse`
    that verb should return. Every invocation appends ``(url, kwargs)`` to
    ``self.calls`` so tests can assert exactly what the adapter built.
    """

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("post", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("get", url, kwargs)

    async def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("put", url, kwargs)

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("delete", url, kwargs)

    async def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("patch", url, kwargs)

    def _record(self, verb: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((verb, url, kwargs))
        return self._responses[verb]


def _adapter(fake: FakeHttpClient) -> AdobeSignESignatureAdapter:
    adapter = AdobeSignESignatureAdapter(api_base=API_BASE, access_token=TOKEN)
    adapter._http = fake  # inject before any method call
    return adapter


def _signature_request() -> SignatureRequest:
    return SignatureRequest(
        document_id="transient-doc-123",
        recipients=[
            Recipient(name="Alice", email="alice@example.com"),
            Recipient(name="Bob", email="bob@example.com"),
        ],
        subject="Loan agreement",
        message="Please review and sign.",
    )


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_builds_request_and_parses_envelope(self) -> None:
        fake = FakeHttpClient({"post": FakeResponse(status_code=201, json_body={"id": "CBJCHBCAABAA-agreement-id"})})
        adapter = _adapter(fake)

        envelope = await adapter.send(_signature_request())

        # (a) outbound request the adapter built
        assert len(fake.calls) == 1
        verb, url, kwargs = fake.calls[0]
        assert verb == "post"
        assert url == f"{API_BASE}/agreements"

        payload = kwargs["json"]
        assert payload["fileInfos"] == [{"transientDocumentId": "transient-doc-123"}]
        assert payload["name"] == "Loan agreement"
        assert payload["message"] == "Please review and sign."
        assert payload["signatureType"] == "ESIGN"
        assert payload["state"] == "IN_PROCESS"
        # recipients become ordered SIGNER participant sets
        assert payload["participantSetsInfo"] == [
            {"memberInfos": [{"email": "alice@example.com"}], "order": 1, "role": "SIGNER"},
            {"memberInfos": [{"email": "bob@example.com"}], "order": 2, "role": "SIGNER"},
        ]

        # auth headers
        headers = kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"

        # (b) parsed domain return type
        assert isinstance(envelope, ESignatureEnvelope)
        assert envelope.provider == "adobe-sign"
        assert envelope.document_id == "transient-doc-123"
        assert envelope.status == ESignatureStatus.SENT
        assert envelope.provider_envelope_id == "CBJCHBCAABAA-agreement-id"
        assert envelope.sent_at is not None

    @pytest.mark.asyncio
    async def test_non_2xx_raises_via_raise_for_status(self) -> None:
        fake = FakeHttpClient({"post": FakeResponse(status_code=400, text="INVALID_FILE_INFO")})
        adapter = _adapter(fake)

        with pytest.raises(_HttpStatusError):
            await adapter.send(_signature_request())

        # the adapter still issued exactly one request before failing
        assert len(fake.calls) == 1
        assert fake.calls[0][0] == "post"


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_maps_signed_status(self) -> None:
        fake = FakeHttpClient({"get": FakeResponse(status_code=200, json_body={"status": "SIGNED"})})
        adapter = _adapter(fake)

        envelope = await adapter.get("agreement-42")

        verb, url, kwargs = fake.calls[0]
        assert verb == "get"
        assert url == f"{API_BASE}/agreements/agreement-42"
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"

        assert envelope is not None
        assert envelope.provider == "adobe-sign"
        assert envelope.provider_envelope_id == "agreement-42"
        assert envelope.status == ESignatureStatus.SIGNED

    @pytest.mark.asyncio
    async def test_maps_out_for_signature_to_sent(self) -> None:
        fake = FakeHttpClient({"get": FakeResponse(status_code=200, json_body={"status": "OUT_FOR_SIGNATURE"})})
        adapter = _adapter(fake)

        envelope = await adapter.get("agreement-99")

        assert envelope is not None
        assert envelope.status == ESignatureStatus.SENT

    @pytest.mark.asyncio
    async def test_404_returns_none(self) -> None:
        fake = FakeHttpClient({"get": FakeResponse(status_code=404, text="not found")})
        adapter = _adapter(fake)

        result = await adapter.get("missing-agreement")

        assert result is None
        # a 404 short-circuits BEFORE raise_for_status, so the request was still made
        assert fake.calls[0][1] == f"{API_BASE}/agreements/missing-agreement"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_success_returns_true_and_sends_cancel_state(self) -> None:
        fake = FakeHttpClient({"put": FakeResponse(status_code=200, json_body={})})
        adapter = _adapter(fake)

        ok = await adapter.cancel("agreement-7")

        assert ok is True
        verb, url, kwargs = fake.calls[0]
        assert verb == "put"
        assert url == f"{API_BASE}/agreements/agreement-7/state"
        assert kwargs["json"]["state"] == "CANCELLED"
        assert kwargs["json"]["agreementCancellationInfo"]["comment"] == "cancelled by app"
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_failure_status_returns_false(self) -> None:
        fake = FakeHttpClient({"put": FakeResponse(status_code=403, text="forbidden")})
        adapter = _adapter(fake)

        ok = await adapter.cancel("agreement-8")

        assert ok is False
