# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Azure Blob Storage document adapter — wraps the official azure-storage-blob SDK."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import Any

from pyfly.ecm.models import Document, DocumentVersion


class AzureBlobStorageAdapter:
    """Stores blobs in an Azure container under ``<prefix>/<document_id>/v<version>``."""

    name = "azure-blob"

    def __init__(
        self,
        container: str,
        *,
        connection_string: str | None = None,
        account_url: str | None = None,
        credential: Any | None = None,
        key_prefix: str = "",
        service: Any | None = None,
    ) -> None:
        self._container = container
        self._connection_string = connection_string
        self._account_url = account_url
        self._credential = credential
        self._prefix = key_prefix.rstrip("/") + "/" if key_prefix else ""
        self._service: Any | None = service

    def _ensure_service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from azure.storage.blob import BlobServiceClient  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "AzureBlobStorageAdapter requires azure-storage-blob — `pip install azure-storage-blob`"
            raise ImportError(msg) from exc
        if self._connection_string is not None:
            self._service = BlobServiceClient.from_connection_string(self._connection_string)
        else:
            assert self._account_url is not None
            self._service = BlobServiceClient(account_url=self._account_url, credential=self._credential)
        return self._service

    async def _run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _key(self, document_id: str, version: int) -> str:
        return f"{self._prefix}{document_id}/v{version}"

    async def upload(self, document: Document, content: bytes) -> DocumentVersion:
        service = self._ensure_service()
        version_no = (document.versions[-1].version + 1) if document.versions else 1
        blob = service.get_blob_client(container=self._container, blob=self._key(document.id, version_no))
        await self._run(blob.upload_blob, content, overwrite=True, content_settings=None)
        return DocumentVersion(
            version=version_no,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            storage_uri=blob.url,
        )

    async def download(self, document: Document, version: int | None = None) -> bytes:
        if not document.versions:
            msg = f"document '{document.id}' has no versions"
            raise FileNotFoundError(msg)
        service = self._ensure_service()
        target_version = version or document.versions[-1].version
        blob = service.get_blob_client(container=self._container, blob=self._key(document.id, target_version))
        stream = await self._run(blob.download_blob)
        data = await self._run(stream.readall)
        return data if isinstance(data, bytes) else bytes(data)

    async def delete(self, document: Document, version: int | None = None) -> bool:
        service = self._ensure_service()
        if version is None:
            for v in document.versions:
                blob = service.get_blob_client(container=self._container, blob=self._key(document.id, v.version))
                with contextlib.suppress(Exception):
                    await self._run(blob.delete_blob)
            return bool(document.versions)
        blob = service.get_blob_client(container=self._container, blob=self._key(document.id, version))
        try:
            await self._run(blob.delete_blob)
            return True
        except Exception:  # noqa: BLE001
            return False
