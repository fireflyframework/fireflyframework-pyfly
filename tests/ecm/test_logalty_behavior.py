# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for ``LogaltyESignatureAdapter`` (mocked HTTP, no network).

A fake pooled HTTP client is injected at ``adapter._http`` so that
``async with await adapter._client() as client:`` yields the fake. Each test
asserts both the outbound request the adapter builds (URL, verb, payload, auth
headers) and how the adapter parses the canned response into its domain types.
"""

from __future__ import annotations

from typing import Any

import pytest

httpx = pytest.importorskip("httpx", reason="httpx not installed (install pyfly[client])")

from pyfly.ecm.adapters.logalty import LogaltyESignatureAdapter
from pyfly.ecm.models import (
    ESignatureEnvelope,
    ESignatureStatus,
    Recipient,
    SignatureRequest,
)

API_BASE = "https://tenant.logalty.example/api/v1"
API_KEY = "secret-key-123"


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(
        self,
        *,
        status_code: int,
        json_body: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers: dict[str, str] = {"content-type": "application/json"}

    def json(self) -> Any:
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
            )


class FakeHttpClient:
    """Records outbound calls and replays a queue of canned responses.

    Implements the verbs the adapter actually invokes (``post``/``get``/``delete``)
    as async methods that capture ``(url, kwargs)`` and pop the next response.
    """

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> FakeResponse:
        if not self._responses:
            raise AssertionError("FakeHttpClient: no more canned responses queued")
        return self._responses.pop(0)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"verb": "POST", "url": url, "kwargs": kwargs})
        return self._next()

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"verb": "GET", "url": url, "kwargs": kwargs})
        return self._next()

    async def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"verb": "DELETE", "url": url, "kwargs": kwargs})
        return self._next()


def _adapter(*responses: FakeResponse) -> tuple[LogaltyESignatureAdapter, FakeHttpClient]:
    adapter = LogaltyESignatureAdapter(api_base=API_BASE, api_key=API_KEY)
    fake = FakeHttpClient(*responses)
    adapter._http = fake  # inject before any method call
    return adapter, fake


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_builds_request_and_parses_envelope(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=201, json_body={"envelopeId": "env-789"}))
        request = SignatureRequest(
            document_id="doc-42",
            recipients=[
                Recipient(name="Alice", email="alice@example.com"),
                Recipient(name="Bob", email="bob@example.com", role="approver"),
            ],
            subject="Sign this",
            message="Please review and sign.",
        )

        envelope = await adapter.send(request)

        # (a) outbound request the adapter built
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["verb"] == "POST"
        assert call["url"] == f"{API_BASE}/envelopes"
        assert call["kwargs"]["headers"]["X-Api-Key"] == API_KEY
        assert call["kwargs"]["headers"]["Content-Type"] == "application/json"
        payload = call["kwargs"]["json"]
        assert payload["documentId"] == "doc-42"
        assert payload["subject"] == "Sign this"
        assert payload["message"] == "Please review and sign."
        assert payload["signers"] == [
            {"name": "Alice", "email": "alice@example.com", "role": "signer"},
            {"name": "Bob", "email": "bob@example.com", "role": "approver"},
        ]

        # (b) parsed domain return type
        assert isinstance(envelope, ESignatureEnvelope)
        assert envelope.provider == "logalty"
        assert envelope.document_id == "doc-42"
        assert envelope.status is ESignatureStatus.SENT
        assert envelope.provider_envelope_id == "env-789"
        assert envelope.sent_at is not None

    @pytest.mark.asyncio
    async def test_error_status_raises(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=422, text="bad request"))
        request = SignatureRequest(
            document_id="doc-1",
            recipients=[Recipient(name="Alice", email="alice@example.com")],
        )

        with pytest.raises(httpx.HTTPStatusError):
            await adapter.send(request)

        assert fake.calls[0]["verb"] == "POST"
        assert fake.calls[0]["url"] == f"{API_BASE}/envelopes"


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_maps_provider_status_to_domain_enum(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=200, json_body={"status": "COMPLETED"}))

        envelope = await adapter.get("env-789")

        call = fake.calls[0]
        assert call["verb"] == "GET"
        assert call["url"] == f"{API_BASE}/envelopes/env-789"
        assert call["kwargs"]["headers"]["X-Api-Key"] == API_KEY

        assert isinstance(envelope, ESignatureEnvelope)
        # "COMPLETED" maps to SIGNED per the adapter's status table.
        assert envelope.status is ESignatureStatus.SIGNED
        assert envelope.provider_envelope_id == "env-789"
        assert envelope.provider == "logalty"

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=404))

        result = await adapter.get("missing")

        assert result is None
        assert fake.calls[0]["url"] == f"{API_BASE}/envelopes/missing"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_returns_true_on_204(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=204))

        ok = await adapter.cancel("env-1")

        assert ok is True
        call = fake.calls[0]
        assert call["verb"] == "DELETE"
        assert call["url"] == f"{API_BASE}/envelopes/env-1"
        assert call["kwargs"]["headers"]["X-Api-Key"] == API_KEY

    @pytest.mark.asyncio
    async def test_returns_false_on_error_status(self) -> None:
        adapter, fake = _adapter(FakeResponse(status_code=409))

        ok = await adapter.cancel("env-2")

        assert ok is False
        assert fake.calls[0]["url"] == f"{API_BASE}/envelopes/env-2"
