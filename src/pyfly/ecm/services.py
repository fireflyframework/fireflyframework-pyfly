# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""High-level ECM services that combine storage + metadata + e-signature ports."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from pyfly.ecm.models import (
    Document,
    DocumentVersion,
    ESignatureEnvelope,
    Folder,
    SignatureRequest,
)
from pyfly.ecm.ports import (
    DocumentStoragePort,
    ESignatureAdapter,
    FolderRepositoryPort,
    MetadataStoragePort,
)


class DocumentService:
    """Orchestrates document upload/download/list across the storage + metadata ports."""

    def __init__(
        self,
        storage: DocumentStoragePort,
        metadata: MetadataStoragePort,
        folders: FolderRepositoryPort | None = None,
    ) -> None:
        self._storage = storage
        self._metadata = metadata
        self._folders = folders

    async def upload(
        self,
        *,
        name: str,
        content: bytes,
        folder_id: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, object] | None = None,
    ) -> Document:
        document = Document(
            name=name,
            folder_id=folder_id,
            content_type=content_type,
            size_bytes=len(content),
            metadata=metadata or {},
        )
        version = await self._storage.upload(document, content)
        document.versions.append(version)
        document.updated_at = datetime.now(UTC)
        return await self._metadata.save(document)

    async def download(self, document_id: str, *, version: int | None = None) -> bytes:
        document = await self._metadata.get(document_id)
        if document is None:
            msg = f"document '{document_id}' not found"
            raise FileNotFoundError(msg)
        return await self._storage.download(document, version)

    async def list(self, *, folder_id: str | None = None) -> list[Document]:
        return await self._metadata.list(folder_id)

    async def delete(self, document_id: str) -> bool:
        document = await self._metadata.get(document_id)
        if document is None:
            return False
        await self._storage.delete(document)
        return await self._metadata.delete(document_id)

    async def create_folder(self, folder: Folder) -> Folder:
        if self._folders is None:
            msg = "no FolderRepositoryPort configured"
            raise RuntimeError(msg)
        return await self._folders.save(folder)

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class ESignatureService:
    def __init__(self, adapter: ESignatureAdapter) -> None:
        self._adapter = adapter

    async def request(self, signature_request: SignatureRequest) -> ESignatureEnvelope:
        return await self._adapter.send(signature_request)

    async def get(self, envelope_id: str) -> ESignatureEnvelope | None:
        return await self._adapter.get(envelope_id)

    async def cancel(self, envelope_id: str) -> bool:
        return await self._adapter.cancel(envelope_id)
