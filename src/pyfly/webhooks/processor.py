# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""WebhookProcessor — main entry point that listeners delegate to."""

from __future__ import annotations

import json
import logging

from pyfly.webhooks.event_listener import (
    AbstractWebhookEventListener,
    InMemoryWebhookEventStore,
    WebhookEvent,
    WebhookEventStore,
)
from pyfly.webhooks.signature import NoOpSignatureValidator, SignatureValidator

_logger = logging.getLogger(__name__)


class WebhookProcessor:
    """Validates, dedupes and dispatches inbound webhook events."""

    def __init__(
        self,
        listeners: list[AbstractWebhookEventListener] | None = None,
        *,
        signature_validators: dict[str, SignatureValidator] | None = None,
        event_store: WebhookEventStore | None = None,
    ) -> None:
        self._listeners: dict[str, list[AbstractWebhookEventListener]] = {}
        for listener in listeners or []:
            self._listeners.setdefault(listener.source, []).append(listener)
        self._validators = signature_validators or {}
        self._store = event_store or InMemoryWebhookEventStore()

    def register(self, listener: AbstractWebhookEventListener) -> None:
        self._listeners.setdefault(listener.source, []).append(listener)

    def register_validator(self, source: str, validator: SignatureValidator) -> None:
        self._validators[source] = validator

    async def process(
        self,
        *,
        source: str,
        raw_body: bytes,
        headers: dict[str, str],
        signature_header: str = "X-Signature",
        idempotency_header: str = "X-Idempotency-Key",
    ) -> WebhookEvent:
        validator = self._validators.get(source, NoOpSignatureValidator())
        if not validator.is_valid(body=raw_body, signature=headers.get(signature_header)):
            msg = f"invalid signature for source '{source}'"
            raise ValueError(msg)

        body: dict[str, object] = {}
        if raw_body:
            try:
                body = json.loads(raw_body)
            except Exception:  # noqa: BLE001
                body = {"_raw": raw_body.decode("utf-8", errors="replace")}

        event = WebhookEvent(
            source=source,
            event_type=str(body.get("type", "unknown")),
            headers=dict(headers),
            body=body,
            raw_body=raw_body,
            idempotency_key=headers.get(idempotency_header),
        )

        if event.idempotency_key:
            if await self._store.already_processed(event.idempotency_key):
                _logger.info("duplicate webhook ignored: %s", event.idempotency_key)
                return event
            await self._store.remember(event.idempotency_key)

        for listener in self._listeners.get(source, []):
            try:
                await listener.handle(event)
            except Exception as exc:  # noqa: BLE001
                _logger.error("webhook listener %s failed: %s", listener.source, exc)
                await listener.on_error(event, exc)
        return event
