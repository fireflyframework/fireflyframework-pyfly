# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Delegated read-only Graph adapters for OneDrive and SharePoint drives."""

from __future__ import annotations

import re
from typing import Any, Literal, Self
from urllib.parse import unquote, urlsplit

from pyfly.agentic.content.sources.dtos import (
    Change,
    ChangePage,
    Cursor,
    DownloadReceipt,
    ItemPage,
    RemoteItem,
    SourceScope,
)
from pyfly.agentic.content.sources.errors import SourceError
from pyfly.agentic.content.sources.parsing import identifier, path_id, resume, rows, size, text
from pyfly.agentic.content.sources.ports import AsyncByteSink, DelegatedTokenSource
from pyfly.agentic.content.sources.transport import SourceTransport, checked_url, redirect_host
from pyfly.client.ports.outbound import StreamingHttpClientPort


class _GraphSource:
    provider: str

    def __init__(
        self,
        scope: SourceScope,
        tokens: DelegatedTokenSource,
        *,
        http_client: StreamingHttpClientPort | None = None,
        owns_http_client: bool = False,
        timeout: float = 30,
        download_hosts: frozenset[str] = frozenset(),
    ) -> None:
        if scope.account.provider != self.provider:
            raise ValueError("Provider identity does not match this adapter")
        self.scope = scope
        self._base = "https://graph.microsoft.com/v1.0/drives/" + path_id(scope.drive_id)
        self._http = SourceTransport(
            scope.account,
            tokens,
            host="graph.microsoft.com",
            http_client=http_client,
            owns_http_client=owns_http_client,
            timeout=timeout,
            download_hosts=download_hosts,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, suffix: str) -> str:
        return self._base + suffix

    def _continuation(self, value: Any, path: str, operation: str) -> Cursor | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise SourceError("invalid_response")
        checked_url(value, host="graph.microsoft.com")
        actual_path, expected_path = urlsplit(value).path, urlsplit(path).path
        function_token = operation == "changes" and re.fullmatch(
            re.escape(unquote(expected_path)) + r"\(token='[A-Za-z0-9._~+=:;,-]+'\)", unquote(actual_path)
        )
        if actual_path != expected_path and not function_token:
            raise SourceError("unsafe_url")
        try:
            return Cursor(self.scope, operation, value)
        except ValueError:
            raise SourceError("invalid_response") from None

    def _item(self, value: dict[str, Any]) -> RemoteItem:
        parent = value.get("parentReference", {})
        if not isinstance(parent, dict) or parent.get("driveId", self.scope.drive_id) != self.scope.drive_id:
            raise SourceError("invalid_response")
        file = value.get("file", {})
        if not isinstance(file, dict):
            raise SourceError("invalid_response")
        kind: Literal["file", "folder", "shortcut"] = (
            "shortcut" if "remoteItem" in value else "folder" if "folder" in value else "file"
        )
        return RemoteItem(
            self.scope,
            identifier(value.get("id")),
            identifier(value.get("name")),
            kind,
            text(value.get("eTag"), optional=True),
            size(value.get("size")),
            text(file.get("mimeType"), optional=True) or "",
            (identifier(parent["id"]),) if "id" in parent else (),
            modified_at=text(value.get("lastModifiedDateTime"), optional=True),
        )

    async def start_cursor(self) -> Cursor:
        page = await self.changes(Cursor(self.scope, "changes", self._url("/root/delta?token=latest")))
        if page.changes or page.next_cursor is not None or page.checkpoint is None:
            raise SourceError("invalid_response")
        return page.checkpoint

    async def changes(self, cursor: Cursor | None = None) -> ChangePage:
        path = self._url("/root/delta")
        token = resume(cursor, self.scope, "changes")
        if token is not None:
            self._continuation(token, path, "changes")
        document = await self._http.json(token or path + "?$top=200")
        changes = tuple(
            Change("deleted", identifier(row.get("id")))
            if "deleted" in row
            else Change("upsert", identifier(row.get("id")), self._item(row))
            for row in rows(document.get("value"))
        )
        next_cursor = self._continuation(document.get("@odata.nextLink"), path, "changes")
        checkpoint = self._continuation(document.get("@odata.deltaLink"), path, "changes")
        if (next_cursor is None) == (checkpoint is None):
            raise SourceError("invalid_response")
        return ChangePage(changes, next_cursor, checkpoint)

    async def children(self, folder_id: str, cursor: Cursor | None = None) -> ItemPage:
        folder_id = identifier(folder_id)
        operation = "children:" + folder_id
        path = self._url("/items/" + path_id(folder_id) + "/children")
        token = resume(cursor, self.scope, operation)
        if token is not None:
            self._continuation(token, path, operation)
        document = await self._http.json(token or path + "?$top=200")
        return ItemPage(
            tuple(self._item(row) for row in rows(document.get("value"))),
            self._continuation(document.get("@odata.nextLink"), path, operation),
        )

    async def get_item(self, item_id: str) -> RemoteItem:
        item_id = identifier(item_id)
        document = await self._http.json(self._url("/items/" + path_id(item_id)))
        item = self._item(document)
        if item.item_id != item_id or "deleted" in document:
            raise SourceError("unavailable")
        return item

    async def check_access(self, item_id: str) -> RemoteItem:
        item = await self.get_item(item_id)
        if item.kind != "file" or item.version is None:
            raise SourceError("unsupported")
        # A successful content request tests the current delegated principal's
        # download authority. It never follows or consumes the signed content.
        async with self._http.request(self._url("/items/" + path_id(item_id) + "/content")) as response:
            if response.status_code in {302, 307}:
                target = response.headers.get("location", "")
                host = redirect_host(target)
                if host is None or host not in self._http.download_hosts:
                    raise SourceError("unsafe_url")
                checked_url(target, host=host)
        return item

    async def download(
        self, item: RemoteItem, sink: AsyncByteSink, *, max_bytes: int, export_mime: str | None = None
    ) -> DownloadReceipt:
        if item.scope != self.scope:
            raise SourceError("scope_mismatch")
        if item.kind != "file" or item.version is None or export_mime is not None:
            raise SourceError("unsupported")
        current = await self.get_item(item.item_id)
        if current.version != item.version:
            raise SourceError("version_changed")
        measured, digest = await self._http.transfer(
            self._url("/items/" + path_id(item.item_id) + "/content"),
            sink,
            max_bytes=max_bytes,
            expected_size=current.size_bytes,
            redirects=True,
        )
        after = await self.get_item(item.item_id)
        if after.version != item.version:
            raise SourceError("version_changed")
        return DownloadReceipt(self.scope, item.item_id, item.version, measured, digest, current.mime_type)


class SharePointSource(_GraphSource):
    """A SharePoint document-library drive, identified by stable Graph drive ID."""

    provider = "sharepoint"


class OneDriveSource(_GraphSource):
    """A OneDrive drive, identified by stable Graph drive ID."""

    provider = "onedrive"
