# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""ECM ports — contracts for storage, metadata, folders, e-signature."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pyfly.ecm.models import (
    Document,
    DocumentVersion,
    ESignatureEnvelope,
    Folder,
    SignatureRequest,
)


@runtime_checkable
class DocumentStoragePort(Protocol):
    name: str

    async def upload(self, document: Document, content: bytes) -> DocumentVersion: ...
    async def download(self, document: Document, version: int | None = None) -> bytes: ...
    async def delete(self, document: Document, version: int | None = None) -> bool: ...


@runtime_checkable
class MetadataStoragePort(Protocol):
    async def save(self, document: Document) -> Document: ...
    async def get(self, document_id: str) -> Document | None: ...
    async def list(self, folder_id: str | None = None, *, limit: int = 100) -> list[Document]: ...
    async def delete(self, document_id: str) -> bool: ...


@runtime_checkable
class FolderRepositoryPort(Protocol):
    async def save(self, folder: Folder) -> Folder: ...
    async def get(self, folder_id: str) -> Folder | None: ...
    async def list(self, parent_id: str | None = None) -> list[Folder]: ...
    async def delete(self, folder_id: str) -> bool: ...


@runtime_checkable
class ESignatureAdapter(Protocol):
    name: str

    async def send(self, request: SignatureRequest) -> ESignatureEnvelope: ...
    async def get(self, envelope_id: str) -> ESignatureEnvelope | None: ...
    async def cancel(self, envelope_id: str) -> bool: ...
