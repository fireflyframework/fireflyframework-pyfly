# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for :class:`DocuSignESignatureAdapter`.

These exercise the adapter end-to-end against a fake pooled HTTP client (no
network, no Docker). They assert BOTH the outbound request the adapter builds
(URL/verb/payload/auth headers) AND that the canned response is parsed into the
correct ``ESignatureEnvelope`` domain object.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.ecm.adapters.docusign import DocuSignESignatureAdapter
from pyfly.ecm.models import (
    ESignatureStatus,
    Recipient,
    SignatureRequest,
)


class _FakeResponse:
    """Stand-in for ``httpx.Response`` covering only what the adapter touches."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class _FakeHttpClient:
    """Records every request and replays a pre-seeded response per verb.

    Set on ``adapter._http`` BEFORE calling a method; the adapter's ``_client()``
    then wraps it in a ``PooledHttpClient`` which yields this instance unchanged.
    """

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _record(self, verb: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((verb, url, kwargs))
        return self._responses[verb]

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return await self._record("post", url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return await self._record("get", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> _FakeResponse:
        return await self._record("put", url, **kwargs)


_BASE_URL = "https://demo.docusign.net/restapi"
_ACCOUNT_ID = "acct-123"
_ACCESS_TOKEN = "tok-abc"


def _adapter(fake: _FakeHttpClient) -> DocuSignESignatureAdapter:
    adapter = DocuSignESignatureAdapter(
        base_url=_BASE_URL + "/",  # trailing slash must be stripped
        account_id=_ACCOUNT_ID,
        access_token=_ACCESS_TOKEN,
    )
    adapter._http = fake  # inject the fake before any I/O
    return adapter


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_builds_request_and_parses_envelope() -> None:
    fake = _FakeHttpClient({"post": _FakeResponse(status_code=201, json_body={"envelopeId": "env-789"})})
    adapter = _adapter(fake)

    request = SignatureRequest(
        document_id="doc-1",
        recipients=[
            Recipient(name="Alice", email="alice@example.com"),
            Recipient(name="Bob", email="bob@example.com"),
        ],
        subject="Sign please",
        message="Kindly review and sign.",
    )

    envelope = await adapter.send(request)

    # (a) outbound request the adapter built
    assert len(fake.calls) == 1
    verb, url, kwargs = fake.calls[0]
    assert verb == "post"
    assert url == f"{_BASE_URL}/v2.1/accounts/{_ACCOUNT_ID}/envelopes"

    headers = kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"

    payload = kwargs["json"]
    assert payload["emailSubject"] == "Sign please"
    assert payload["emailBlurb"] == "Kindly review and sign."
    assert payload["status"] == "sent"
    assert payload["documents"][0]["documentId"] == "doc-1"
    assert payload["documents"][0]["fileExtension"] == "pdf"
    signers = payload["recipients"]["signers"]
    assert [s["email"] for s in signers] == ["alice@example.com", "bob@example.com"]
    assert [s["name"] for s in signers] == ["Alice", "Bob"]
    # recipientId / routingOrder are 1-based strings
    assert [s["recipientId"] for s in signers] == ["1", "2"]
    assert [s["routingOrder"] for s in signers] == ["1", "2"]

    # (b) parsed domain return type
    assert envelope.provider == "docusign"
    assert envelope.document_id == "doc-1"
    assert envelope.status is ESignatureStatus.SENT
    assert envelope.provider_envelope_id == "env-789"
    assert envelope.sent_at is not None


@pytest.mark.asyncio
async def test_send_raises_on_error_status() -> None:
    fake = _FakeHttpClient({"post": _FakeResponse(status_code=401, json_body={"errorCode": "AUTH"})})
    adapter = _adapter(fake)
    request = SignatureRequest(
        document_id="doc-err",
        recipients=[Recipient(name="Carol", email="carol@example.com")],
    )

    with pytest.raises(RuntimeError):
        await adapter.send(request)

    # the adapter still issued exactly one POST before raising
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "post"


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parses_completed_envelope() -> None:
    fake = _FakeHttpClient(
        {
            "get": _FakeResponse(
                status_code=200,
                json_body={
                    "status": "completed",
                    "sentDateTime": "2026-06-01T10:00:00Z",
                    "completedDateTime": "2026-06-02T12:30:00Z",
                },
            )
        }
    )
    adapter = _adapter(fake)

    envelope = await adapter.get("env-555")

    # (a) outbound request
    verb, url, kwargs = fake.calls[0]
    assert verb == "get"
    assert url == f"{_BASE_URL}/v2.1/accounts/{_ACCOUNT_ID}/envelopes/env-555"
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"

    # (b) "completed" maps to SIGNED, both timestamps parsed
    assert envelope is not None
    assert envelope.status is ESignatureStatus.SIGNED
    assert envelope.provider_envelope_id == "env-555"
    assert envelope.sent_at is not None
    assert envelope.sent_at.year == 2026
    assert envelope.signed_at is not None
    assert envelope.signed_at.day == 2


@pytest.mark.asyncio
async def test_get_returns_none_on_404() -> None:
    fake = _FakeHttpClient({"get": _FakeResponse(status_code=404, json_body={})})
    adapter = _adapter(fake)

    result = await adapter.get("missing")

    assert result is None
    assert fake.calls[0][0] == "get"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_voids_envelope_and_returns_true() -> None:
    fake = _FakeHttpClient({"put": _FakeResponse(status_code=200, json_body={})})
    adapter = _adapter(fake)

    ok = await adapter.cancel("env-321")

    verb, url, kwargs = fake.calls[0]
    assert verb == "put"
    assert url == f"{_BASE_URL}/v2.1/accounts/{_ACCOUNT_ID}/envelopes/env-321"
    assert kwargs["json"] == {
        "status": "voided",
        "voidedReason": "cancelled by application",
    }
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert ok is True


@pytest.mark.asyncio
async def test_cancel_returns_false_on_non_200() -> None:
    fake = _FakeHttpClient({"put": _FakeResponse(status_code=409, json_body={})})
    adapter = _adapter(fake)

    ok = await adapter.cancel("env-409")

    assert ok is False
    assert fake.calls[0][0] == "put"
