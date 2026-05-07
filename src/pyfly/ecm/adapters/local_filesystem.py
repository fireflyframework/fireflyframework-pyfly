# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Local-filesystem document storage adapter — useful for dev / tests."""

from __future__ import annotations

import asyncio
import hashlib
import pathlib

from pyfly.ecm.models import Document, DocumentVersion


class LocalFilesystemStorageAdapter:
    """Stores document blobs as files under ``<base_dir>/<document_id>/v<version>``."""

    name = "local-filesystem"

    def __init__(self, base_dir: str | pathlib.Path) -> None:
        self._base = pathlib.Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    async def upload(self, document: Document, content: bytes) -> DocumentVersion:
        version_no = (document.versions[-1].version + 1) if document.versions else 1
        target = self._path_for(document.id, version_no)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.get_event_loop().run_in_executor(None, target.write_bytes, content)
        return DocumentVersion(
            version=version_no,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            storage_uri=str(target),
        )

    async def download(self, document: Document, version: int | None = None) -> bytes:
        if not document.versions:
            msg = f"document '{document.id}' has no versions"
            raise FileNotFoundError(msg)
        target_version = version or document.versions[-1].version
        target = self._path_for(document.id, target_version)
        return await asyncio.get_event_loop().run_in_executor(None, target.read_bytes)

    async def delete(self, document: Document, version: int | None = None) -> bool:
        if version is None:
            removed = False
            for v in document.versions:
                target = self._path_for(document.id, v.version)
                if target.exists():
                    await asyncio.get_event_loop().run_in_executor(None, target.unlink)
                    removed = True
            return removed
        target = self._path_for(document.id, version)
        if not target.exists():
            return False
        await asyncio.get_event_loop().run_in_executor(None, target.unlink)
        return True

    def _path_for(self, document_id: str, version: int) -> pathlib.Path:
        return self._base / document_id / f"v{version}"
