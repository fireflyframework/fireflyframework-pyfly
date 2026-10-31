# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral immutable archive and delivery-projection ports."""

from typing import Protocol, runtime_checkable

from pyfly.audit.archive_models import ArchiveCapabilities, ArchiveDeliveryReceipt, ArchiveReceipt
from pyfly.audit.ports import AuditError


class ArchiveError(AuditError):
    """Safe delivery failures contain no third-party adapter errors or credential material."""


@runtime_checkable
class ArchiveSink(Protocol):
    def capabilities(self) -> ArchiveCapabilities: ...
    async def put(self, content_id: str, content: bytes) -> ArchiveReceipt: ...
    async def get(self, receipt: ArchiveReceipt) -> bytes: ...


@runtime_checkable
class ArchiveReceiptStore(Protocol):
    async def record(self, delivery_id: str, receipt: ArchiveDeliveryReceipt) -> None: ...
    async def lookup(self, delivery_id: str) -> ArchiveDeliveryReceipt | None: ...
