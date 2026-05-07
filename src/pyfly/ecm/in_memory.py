# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""In-memory MetadataStoragePort + FolderRepositoryPort adapters."""

from __future__ import annotations

import asyncio

from pyfly.ecm.models import Document, Folder


class InMemoryMetadataStorage:
    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._lock = asyncio.Lock()

    async def save(self, document: Document) -> Document:
        async with self._lock:
            self._docs[document.id] = document
        return document

    async def get(self, document_id: str) -> Document | None:
        async with self._lock:
            return self._docs.get(document_id)

    async def list(self, folder_id: str | None = None, *, limit: int = 100) -> list[Document]:
        async with self._lock:
            results = list(self._docs.values())
        if folder_id is not None:
            results = [d for d in results if d.folder_id == folder_id]
        return results[:limit]

    async def delete(self, document_id: str) -> bool:
        async with self._lock:
            return self._docs.pop(document_id, None) is not None


class InMemoryFolderRepository:
    def __init__(self) -> None:
        self._folders: dict[str, Folder] = {}
        self._lock = asyncio.Lock()

    async def save(self, folder: Folder) -> Folder:
        async with self._lock:
            self._folders[folder.id] = folder
        return folder

    async def get(self, folder_id: str) -> Folder | None:
        async with self._lock:
            return self._folders.get(folder_id)

    async def list(self, parent_id: str | None = None) -> list[Folder]:
        async with self._lock:
            return [f for f in self._folders.values() if f.parent_id == parent_id]

    async def delete(self, folder_id: str) -> bool:
        async with self._lock:
            return self._folders.pop(folder_id, None) is not None
