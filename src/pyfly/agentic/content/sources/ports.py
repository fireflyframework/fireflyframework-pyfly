# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Optional v2 ports; v1 ContentSource implementations remain unchanged."""

from typing import Protocol, runtime_checkable

from pyfly.agentic.content.sources.dtos import Account, ChangePage, Cursor, DownloadReceipt, ItemPage, RemoteItem


class DelegatedTokenSource(Protocol):
    async def access_token(self, account: Account) -> str:
        """Return a current delegated token for exactly this verified account.

        The application owns encrypted refresh custody, rotation synchronization,
        and the association with its requesting user. No application fallback.
        """
        ...


class AsyncByteSink(Protocol):
    async def write(self, chunk: bytes) -> None:
        """Stage one bounded chunk; do not publish until download returns a receipt.

        Caller owns abort/cleanup after cancellation or any transfer failure.
        """
        ...


@runtime_checkable
class ContentSourceV2(Protocol):
    async def start_cursor(self) -> Cursor:
        """Capture a future-change bookmark; provider initial-scan rules still apply."""
        ...

    async def changes(self, cursor: Cursor | None = None) -> ChangePage: ...
    async def children(self, folder_id: str, cursor: Cursor | None = None) -> ItemPage: ...
    async def get_item(self, item_id: str) -> RemoteItem: ...
    async def check_access(self, item_id: str) -> RemoteItem:
        """Recheck this account's current content permission, not remote ACL entries."""
        ...

    async def download(
        self, item: RemoteItem, sink: AsyncByteSink, *, max_bytes: int, export_mime: str | None = None
    ) -> DownloadReceipt: ...
