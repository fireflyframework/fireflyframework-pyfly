# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the webhooks module."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from pyfly.webhooks.event_listener import AbstractWebhookEventListener, WebhookEvent
from pyfly.webhooks.processor import WebhookProcessor
from pyfly.webhooks.signature import HmacSignatureValidator


class StubListener(AbstractWebhookEventListener):
    source = "stripe"

    def __init__(self) -> None:
        self.events: list[WebhookEvent] = []

    async def handle(self, event: WebhookEvent) -> None:
        self.events.append(event)


def _signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_processor_validates_signature_and_dispatches() -> None:
    listener = StubListener()
    processor = WebhookProcessor(
        listeners=[listener],
        signature_validators={"stripe": HmacSignatureValidator("topsecret")},
    )
    body = json.dumps({"type": "payment.succeeded"}).encode("utf-8")
    headers = {"X-Signature": _signature(b"topsecret", body)}
    event = await processor.process(source="stripe", raw_body=body, headers=headers)
    assert event.event_type == "payment.succeeded"
    assert listener.events and listener.events[0].event_type == "payment.succeeded"


@pytest.mark.asyncio
async def test_processor_rejects_invalid_signature() -> None:
    processor = WebhookProcessor(signature_validators={"stripe": HmacSignatureValidator("secret")})
    with pytest.raises(ValueError, match="invalid signature"):
        await processor.process(
            source="stripe",
            raw_body=b"{}",
            headers={"X-Signature": "sha256=00"},
        )


@pytest.mark.asyncio
async def test_processor_dedupes_idempotency_keys() -> None:
    listener = StubListener()
    processor = WebhookProcessor(listeners=[listener])
    headers = {"X-Idempotency-Key": "abc"}
    await processor.process(source="stripe", raw_body=b"{}", headers=headers)
    await processor.process(source="stripe", raw_body=b"{}", headers=headers)
    assert len(listener.events) == 1
