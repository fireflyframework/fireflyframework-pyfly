# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the ECM module."""

from __future__ import annotations

import pathlib

import pytest

from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter
from pyfly.ecm.adapters.noop_esignature import NoOpESignatureAdapter
from pyfly.ecm.in_memory import InMemoryFolderRepository, InMemoryMetadataStorage
from pyfly.ecm.models import ESignatureStatus, Recipient, SignatureRequest
from pyfly.ecm.services import DocumentService, ESignatureService


@pytest.mark.asyncio
async def test_upload_download(tmp_path: pathlib.Path) -> None:
    storage = LocalFilesystemStorageAdapter(tmp_path)
    metadata = InMemoryMetadataStorage()
    folders = InMemoryFolderRepository()
    service = DocumentService(storage=storage, metadata=metadata, folders=folders)
    document = await service.upload(name="readme.md", content=b"hello", content_type="text/markdown")
    fetched = await service.download(document.id)
    assert fetched == b"hello"


@pytest.mark.asyncio
async def test_list_filters_by_folder(tmp_path: pathlib.Path) -> None:
    service = DocumentService(
        storage=LocalFilesystemStorageAdapter(tmp_path),
        metadata=InMemoryMetadataStorage(),
        folders=InMemoryFolderRepository(),
    )
    await service.upload(name="a.txt", content=b"a", folder_id="f-1")
    await service.upload(name="b.txt", content=b"b", folder_id="f-2")
    in_f1 = await service.list(folder_id="f-1")
    assert len(in_f1) == 1


@pytest.mark.asyncio
async def test_esignature_round_trip() -> None:
    adapter = NoOpESignatureAdapter()
    service = ESignatureService(adapter=adapter)
    envelope = await service.request(
        SignatureRequest(
            document_id="d-1",
            recipients=[Recipient(name="Alice", email="a@x.com")],
        )
    )
    assert envelope.status == ESignatureStatus.SIGNED
    fetched = await service.get(envelope.id)
    assert fetched is not None
    assert await service.cancel(envelope.id)
