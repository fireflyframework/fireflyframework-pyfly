# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Abstract base class for webhook event consumers + idempotency store."""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


@dataclass
class WebhookEvent:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = ""
    event_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    raw_body: bytes = b""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    idempotency_key: str | None = None


@runtime_checkable
class WebhookEventStore(Protocol):
    async def already_processed(self, idempotency_key: str) -> bool: ...
    async def remember(self, idempotency_key: str) -> None: ...


class InMemoryWebhookEventStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()

    async def already_processed(self, idempotency_key: str) -> bool:
        async with self._lock:
            return idempotency_key in self._seen

    async def remember(self, idempotency_key: str) -> None:
        async with self._lock:
            self._seen.add(idempotency_key)


class AbstractWebhookEventListener(ABC):
    """Subclass and implement :meth:`handle` to process incoming events."""

    source: str = "default"
    """Free-form source identifier shown in metrics / logs."""

    @abstractmethod
    async def handle(self, event: WebhookEvent) -> None: ...

    async def on_error(self, event: WebhookEvent, error: BaseException) -> None:
        # Default: no-op. Subclasses can override to log / DLQ.
        return None
