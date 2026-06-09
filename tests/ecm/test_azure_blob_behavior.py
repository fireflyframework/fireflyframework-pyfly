# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for :class:`AzureBlobStorageAdapter`.

These exercise the adapter end-to-end against a *fake* azure-storage-blob-style service
injected via the ``service=`` constructor parameter — no network, no Docker, no real SDK.
Each test asserts both halves of the contract:

* the outbound SDK call the adapter built (container, blob name, content payload), and
* how the adapter parsed the result into its domain return types
  (``DocumentVersion``, raw ``bytes``, ``bool``).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter
from pyfly.ecm.models import Document, DocumentVersion


class _FakeDownloadStream:
    """Mimics the stream returned by ``blob.download_blob()`` — a sync ``.readall()``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload


class _FakeBlobClient:
    """Minimal stand-in for an Azure ``BlobClient``.

    Records every call into ``self.calls`` as ``(method_name, args, kwargs)`` tuples
    and returns canned responses. ``delete_blob`` can be told to raise.
    """

    def __init__(
        self,
        *,
        download_payload: bytes = b"",
        delete_raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._download_payload = download_payload
        self._delete_raises = delete_raises
        self.url: str = "https://x.blob.core.windows.net/container/blob"

    def upload_blob(self, data: bytes, /, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upload_blob", (data,), kwargs))
        return {}

    def download_blob(self) -> _FakeDownloadStream:
        self.calls.append(("download_blob", (), {}))
        return _FakeDownloadStream(self._download_payload)

    def delete_blob(self) -> None:
        self.calls.append(("delete_blob", (), {}))
        if self._delete_raises is not None:
            raise self._delete_raises


class _FakeBlobServiceClient:
    """Minimal stand-in for ``BlobServiceClient``.

    Records ``get_blob_client`` calls and returns a shared ``_FakeBlobClient``.
    """

    def __init__(self, blob_client: _FakeBlobClient) -> None:
        self._blob_client = blob_client
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_blob_client(self, **kwargs: Any) -> _FakeBlobClient:
        self.calls.append(("get_blob_client", kwargs))
        return self._blob_client


def _doc(**overrides: Any) -> Document:
    base: dict[str, Any] = {
        "id": "doc-xyz",
        "name": "contract.pdf",
        "content_type": "application/pdf",
    }
    base.update(overrides)
    return Document(**base)


def _adapter(service: _FakeBlobServiceClient, key_prefix: str = "") -> AzureBlobStorageAdapter:
    return AzureBlobStorageAdapter(
        "my-container",
        account_url="https://x.blob.core.windows.net",
        key_prefix=key_prefix,
        service=service,
    )


# ---------------------------------------------------------------------------
# upload — request construction + DocumentVersion parsing
# ---------------------------------------------------------------------------


class TestUpload:
    @pytest.mark.asyncio
    async def test_first_upload_builds_v1_and_returns_version(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)
        document = _doc()
        content = b"hello world"

        result = await adapter.upload(document, content)

        # (a) get_blob_client called with the right container + blob key
        assert len(service.calls) == 1
        method, kwargs = service.calls[0]
        assert method == "get_blob_client"
        assert kwargs["container"] == "my-container"
        assert kwargs["blob"] == "doc-xyz/v1"

        # (b) upload_blob called with the content and overwrite=True
        assert len(blob_client.calls) == 1
        bc_method, bc_args, bc_kwargs = blob_client.calls[0]
        assert bc_method == "upload_blob"
        assert bc_args[0] == content
        assert bc_kwargs.get("overwrite") is True

        # (c) parsed domain return type
        assert isinstance(result, DocumentVersion)
        assert result.version == 1
        assert result.size_bytes == len(content)
        assert result.content_hash == hashlib.sha256(content).hexdigest()

    @pytest.mark.asyncio
    async def test_upload_honors_key_prefix_and_increments_version(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service, key_prefix="tenants/acme/")
        document = _doc(
            versions=[
                DocumentVersion(
                    version=1,
                    content_hash="x",
                    size_bytes=3,
                    storage_uri="https://x.blob.core.windows.net/my-container/tenants/acme/doc-xyz/v1",
                )
            ]
        )

        result = await adapter.upload(document, b"second upload")

        _method, kwargs = service.calls[0]
        assert kwargs["blob"] == "tenants/acme/doc-xyz/v2"
        assert result.version == 2


# ---------------------------------------------------------------------------
# download — request construction + bytes parsing
# ---------------------------------------------------------------------------


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_latest_reads_stream(self) -> None:
        payload = b"PDF-BYTES"
        blob_client = _FakeBlobClient(download_payload=payload)
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="uri1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="uri2"),
            ]
        )

        body = await adapter.download(document)

        # (a) get_blob_client targeted the latest version key
        _method, kwargs = service.calls[0]
        assert kwargs["container"] == "my-container"
        assert kwargs["blob"] == "doc-xyz/v2"

        # (b) download_blob called, then readall consumed into bytes
        assert blob_client.calls[0][0] == "download_blob"
        assert body == payload
        assert isinstance(body, bytes)

    @pytest.mark.asyncio
    async def test_download_explicit_version_targets_that_key(self) -> None:
        blob_client = _FakeBlobClient(download_payload=b"v1-bytes")
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="uri1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="uri2"),
            ]
        )

        body = await adapter.download(document, version=1)

        _method, kwargs = service.calls[0]
        assert kwargs["blob"] == "doc-xyz/v1"
        assert body == b"v1-bytes"

    @pytest.mark.asyncio
    async def test_download_without_versions_raises(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)

        with pytest.raises(FileNotFoundError):
            await adapter.download(_doc(versions=[]))

        assert service.calls == []


# ---------------------------------------------------------------------------
# delete — request construction + bool result
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_all_versions_issues_one_call_each(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="uri1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="uri2"),
            ]
        )

        ok = await adapter.delete(document)

        assert ok is True
        blobs = [kwargs["blob"] for _method, kwargs in service.calls]
        assert blobs == ["doc-xyz/v1", "doc-xyz/v2"]

    @pytest.mark.asyncio
    async def test_delete_single_version_targets_that_key(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)

        ok = await adapter.delete(_doc(versions=[]), version=3)

        assert ok is True
        assert len(service.calls) == 1
        _method, kwargs = service.calls[0]
        assert kwargs["container"] == "my-container"
        assert kwargs["blob"] == "doc-xyz/v3"
        assert blob_client.calls[0][0] == "delete_blob"

    @pytest.mark.asyncio
    async def test_delete_no_versions_returns_false(self) -> None:
        blob_client = _FakeBlobClient()
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)

        ok = await adapter.delete(_doc(versions=[]))

        assert ok is False
        assert service.calls == []

    @pytest.mark.asyncio
    async def test_delete_error_returns_false(self) -> None:
        blob_client = _FakeBlobClient(delete_raises=RuntimeError("BlobNotFound"))
        service = _FakeBlobServiceClient(blob_client)
        adapter = _adapter(service)

        ok = await adapter.delete(_doc(versions=[]), version=1)

        assert ok is False
        assert blob_client.calls[0][0] == "delete_blob"
