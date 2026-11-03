# Copyright 2026 Firefly Software Foundation
# Licensed under the Apache License, Version 2.0.
"""Delegated Google Drive originals and explicit Google Workspace exports."""

from __future__ import annotations

from typing import Any, Literal, Self
from urllib.parse import urlencode

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
from pyfly.agentic.content.sources.transport import SourceTransport
from pyfly.client.ports.outbound import StreamingHttpClientPort

_FIELDS = "id,name,mimeType,size,version,parents,driveId,trashed,modifiedTime,capabilities(canDownload)"
_EXPORTS = {
    "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}


class GoogleDriveSource:
    """Use drive_id='my-drive' for personal storage, otherwise an explicit shared-drive ID."""

    def __init__(
        self,
        scope: SourceScope,
        tokens: DelegatedTokenSource,
        *,
        http_client: StreamingHttpClientPort | None = None,
        owns_http_client: bool = False,
        timeout: float = 30,
    ) -> None:
        if scope.account.provider != "google_drive":
            raise ValueError("Provider identity does not match this adapter")
        self.scope = scope
        self._http = SourceTransport(
            scope.account,
            tokens,
            host="www.googleapis.com",
            http_client=http_client,
            owns_http_client=owns_http_client,
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, path: str, **params: Any) -> str:
        return "https://www.googleapis.com/drive/v3/" + path + "?" + urlencode(params)

    def _drive(self) -> dict[str, str]:
        return {"driveId": self.scope.drive_id} if self.scope.drive_id != "my-drive" else {}

    def _cursor(self, value: Any, operation: str) -> Cursor | None:
        if value is None:
            return None
        try:
            return Cursor(self.scope, operation, value)
        except (ValueError, TypeError):
            raise SourceError("invalid_response") from None

    def _item(self, value: dict[str, Any]) -> RemoteItem:
        drive = value.get("driveId", "my-drive")
        if drive != self.scope.drive_id:
            raise SourceError("scope_mismatch")
        mime = identifier(value.get("mimeType"))
        parents = value.get("parents", [])
        capabilities = value.get("capabilities", {})
        if not isinstance(parents, list) or len(parents) > 100 or not isinstance(capabilities, dict):
            raise SourceError("invalid_response")
        downloadable = capabilities.get("canDownload")
        if downloadable is not None and not isinstance(downloadable, bool):
            raise SourceError("invalid_response")
        kind: Literal["file", "folder", "shortcut"] = (
            "folder"
            if mime == "application/vnd.google-apps.folder"
            else "shortcut"
            if mime == "application/vnd.google-apps.shortcut"
            else "file"
        )
        return RemoteItem(
            self.scope,
            identifier(value.get("id")),
            identifier(value.get("name")),
            kind,
            text(value.get("version"), optional=True),
            size(value.get("size")),
            mime,
            tuple(identifier(parent) for parent in parents),
            downloadable,
            text(value.get("modifiedTime"), optional=True),
        )

    async def start_cursor(self) -> Cursor:
        value = await self._http.json(self._url("changes/startPageToken", supportsAllDrives="true", **self._drive()))
        cursor = self._cursor(value.get("startPageToken"), "changes")
        if cursor is None:
            raise SourceError("invalid_response")
        return cursor

    async def changes(self, cursor: Cursor | None = None) -> ChangePage:
        token = resume(cursor, self.scope, "changes")
        if token is None:
            raise SourceError("invalid_cursor")
        document = await self._http.json(
            self._url(
                "changes",
                pageToken=token,
                pageSize=200,
                supportsAllDrives="true",
                includeItemsFromAllDrives="true" if self._drive() else "false",
                restrictToMyDrive="false" if self._drive() else "true",
                includeRemoved="true",
                fields=f"nextPageToken,newStartPageToken,changes(fileId,removed,changeType,file({_FIELDS}))",
                **self._drive(),
            )
        )
        changes = []
        for row in rows(document.get("changes")):
            if row.get("changeType") != "file":
                raise SourceError("resync_required")
            item_id = identifier(row.get("fileId"))
            if row.get("removed") is True:
                changes.append(Change("removed", item_id))
            else:
                value = row.get("file")
                if not isinstance(value, dict):
                    raise SourceError("invalid_response")
                item = self._item(value)
                if item.item_id != item_id:
                    raise SourceError("invalid_response")
                changes.append(
                    Change("removed", item_id) if value.get("trashed") is True else Change("upsert", item_id, item)
                )
        next_cursor = self._cursor(document.get("nextPageToken"), "changes")
        checkpoint = self._cursor(document.get("newStartPageToken"), "changes")
        if (next_cursor is None) == (checkpoint is None):
            raise SourceError("invalid_response")
        return ChangePage(tuple(changes), next_cursor, checkpoint)

    async def children(self, folder_id: str, cursor: Cursor | None = None) -> ItemPage:
        folder_id = identifier(folder_id)
        operation = "children:" + folder_id
        token = resume(cursor, self.scope, operation)
        escaped = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        options = {"pageToken": token} if token is not None else {}
        document = await self._http.json(
            self._url(
                "files",
                q=f"'{escaped}' in parents and trashed = false",
                pageSize=200,
                supportsAllDrives="true",
                includeItemsFromAllDrives="true",
                corpora="drive" if self._drive() else "user",
                fields=f"nextPageToken,incompleteSearch,files({_FIELDS})",
                **self._drive(),
                **options,
            )
        )
        if document.get("incompleteSearch") is True:
            raise SourceError("resync_required")
        return ItemPage(
            tuple(self._item(row) for row in rows(document.get("files"))),
            self._cursor(document.get("nextPageToken"), operation),
        )

    async def get_item(self, item_id: str) -> RemoteItem:
        item_id = identifier(item_id)
        value = await self._http.json(self._url("files/" + path_id(item_id), supportsAllDrives="true", fields=_FIELDS))
        item = self._item(value)
        if item.item_id != item_id or value.get("trashed") is True:
            raise SourceError("unavailable")
        return item

    async def check_access(self, item_id: str) -> RemoteItem:
        item = await self.get_item(item_id)
        if item.kind != "file" or item.version is None:
            raise SourceError("unsupported")
        if item.can_download is not True:
            raise SourceError("access_denied")
        return item

    async def download(
        self, item: RemoteItem, sink: AsyncByteSink, *, max_bytes: int, export_mime: str | None = None
    ) -> DownloadReceipt:
        if item.scope != self.scope:
            raise SourceError("scope_mismatch")
        current = await self.check_access(item.item_id)
        if item.version is None or current.version != item.version:
            raise SourceError("version_changed")
        native = current.mime_type.startswith("application/vnd.google-apps.")
        if native:
            if export_mime is None or _EXPORTS.get(current.mime_type) != export_mime:
                raise SourceError("unsupported")
            url = self._url("files/" + path_id(item.item_id) + "/export", mimeType=export_mime)
            max_bytes = min(max_bytes, 10_000_000)
        else:
            if export_mime is not None:
                raise SourceError("unsupported")
            url = self._url("files/" + path_id(item.item_id), alt="media", supportsAllDrives="true")
        measured, digest = await self._http.transfer(
            url, sink, max_bytes=max_bytes, expected_size=None if native else current.size_bytes
        )
        after = await self.check_access(item.item_id)
        if after.version != item.version:
            raise SourceError("version_changed")
        return DownloadReceipt(
            self.scope, item.item_id, item.version, measured, digest, export_mime or current.mime_type
        )
