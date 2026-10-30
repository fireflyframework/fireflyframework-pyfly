# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Google Cloud Storage document adapter using the official storage SDK."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from pyfly.ecm.models import Document, DocumentVersion


class GoogleCloudStorageAdapter:
    """Store document versions under ``<prefix>/<document_id>/v<version>``.

    A version is created once using a generation precondition. An ambiguous
    upload is not retried by this adapter or the SDK. Callers retain their
    intended version and perform explicit readback before deciding what to do.
    """

    name = "gcs"

    def __init__(
        self, bucket: str, *, project: str | None = None, key_prefix: str = "", client: Any | None = None
    ) -> None:
        if not bucket:
            raise ValueError("Google Cloud Storage requires a bucket")
        self._bucket, self._project = bucket, project
        self._prefix = key_prefix.rstrip("/") + "/" if key_prefix else ""
        self._client: Any | None = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import storage  # type: ignore[import-untyped, import-not-found, unused-ignore]
            except ImportError as exc:
                raise ImportError("GoogleCloudStorageAdapter requires `pip install pyfly[ecm-gcs]`") from exc
            self._client = storage.Client(project=self._project)
        return self._client

    async def _run(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: function(*args, **kwargs))

    def _key(self, document_id: str, version: int) -> str:
        return f"{self._prefix}{document_id}/v{version}"

    @staticmethod
    def _version(document: Document, version: int | None) -> int:
        if not document.versions:
            raise FileNotFoundError("Document has no retained storage versions")
        selected = document.versions[-1].version if version is None else version
        if type(selected) is not int or selected < 1:
            raise ValueError("A document version must be a positive integer")
        if not any(item.version == selected for item in document.versions):
            raise FileNotFoundError("Document does not retain the requested version")
        return selected

    async def upload(self, document: Document, content: bytes) -> DocumentVersion:
        version = self._version(document, None) + 1 if document.versions else 1
        key = self._key(document.id, version)
        blob = self._ensure_client().bucket(self._bucket).blob(key)
        await self._run(
            blob.upload_from_string, content, content_type=document.content_type, if_generation_match=0, retry=None
        )
        return DocumentVersion(
            version=version,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            storage_uri=f"gs://{self._bucket}/{key}",
        )

    async def download(self, document: Document, version: int | None = None) -> bytes:
        selected = self._version(document, version)
        blob = self._ensure_client().bucket(self._bucket).blob(self._key(document.id, selected))
        data = await self._run(blob.download_as_bytes, retry=None)
        return data if isinstance(data, bytes) else bytes(data)

    async def _delete(self, document: Document, version: int) -> bool:
        blob = self._ensure_client().bucket(self._bucket).blob(self._key(document.id, version))
        try:
            await self._run(blob.delete, retry=None)
        except Exception as exc:
            if isinstance(exc, FileNotFoundError) or getattr(exc, "code", None) == 404:
                return False
            raise
        return True

    async def delete(self, document: Document, version: int | None = None) -> bool:
        if version is not None:
            try:
                selected = self._version(document, version)
            except FileNotFoundError:
                return False
            return await self._delete(document, selected)
        removed = False
        for retained in document.versions:
            removed = await self._delete(document, self._version(document, retained.version)) or removed
        return removed
