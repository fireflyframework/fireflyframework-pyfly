# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Behavior tests for :class:`AwsS3StorageAdapter`.

These exercise the adapter end-to-end against a *fake* boto3-style ``s3``
client injected via the ``client=`` constructor parameter — no network, no
Docker, no real boto3. Each test asserts both halves of the contract:

* the outbound boto3 call the adapter built (method, ``Bucket``/``Key``,
  ``Body``/``ContentType`` payload fields), and
* how the adapter parsed the canned response into its domain return types
  (``DocumentVersion``, raw ``bytes``, ``bool``).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter
from pyfly.ecm.models import Document, DocumentVersion


class _StreamingBody:
    """Mimics botocore's StreamingBody — a one-shot ``.read()`` over bytes."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeS3Client:
    """A minimal boto3 ``s3`` client double.

    Records every call into ``self.calls`` as ``(method_name, kwargs)`` tuples
    and returns canned responses. ``delete_object`` can be told to raise to
    drive the adapter's error path.
    """

    def __init__(
        self,
        *,
        get_payload: bytes = b"",
        delete_raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._get_payload = get_payload
        self._delete_raises = delete_raises

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        # A real S3 PutObject returns an ETag + version metadata.
        return {"ETag": '"d41d8cd98f00b204e9800998ecf8427e"', "VersionId": "null"}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        return {
            "Body": _StreamingBody(self._get_payload),
            "ContentLength": len(self._get_payload),
            "ContentType": "application/octet-stream",
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        if self._delete_raises is not None:
            raise self._delete_raises
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}


def _doc(**overrides: Any) -> Document:
    base: dict[str, Any] = {
        "id": "doc-abc",
        "name": "report.pdf",
        "content_type": "application/pdf",
    }
    base.update(overrides)
    return Document(**base)


# ---------------------------------------------------------------------------
# upload — request construction + DocumentVersion parsing
# ---------------------------------------------------------------------------


class TestUpload:
    @pytest.mark.asyncio
    async def test_first_upload_builds_v1_put_and_returns_version(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("my-bucket", region="eu-west-1", client=client)
        document = _doc()
        content = b"hello world"

        result = await adapter.upload(document, content)

        # (a) outbound request: exactly one put_object with the right payload.
        assert len(client.calls) == 1
        method, kwargs = client.calls[0]
        assert method == "put_object"
        assert kwargs["Bucket"] == "my-bucket"
        assert kwargs["Key"] == "doc-abc/v1"
        assert kwargs["Body"] == content
        assert kwargs["ContentType"] == "application/pdf"

        # (b) parsed domain return type.
        assert isinstance(result, DocumentVersion)
        assert result.version == 1
        assert result.size_bytes == len(content)
        assert result.content_hash == hashlib.sha256(content).hexdigest()
        assert result.storage_uri == "s3://my-bucket/doc-abc/v1"

    @pytest.mark.asyncio
    async def test_upload_honors_key_prefix_and_increments_version(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("bkt", key_prefix="tenants/acme/", client=client)
        # Existing version 1 means the next upload must become version 2.
        document = _doc(
            versions=[
                DocumentVersion(
                    version=1,
                    content_hash="x",
                    size_bytes=3,
                    storage_uri="s3://bkt/tenants/acme/doc-abc/v1",
                )
            ]
        )

        result = await adapter.upload(document, b"second")

        _method, kwargs = client.calls[0]
        # Prefix is normalized to a single trailing slash and prepended to the key.
        assert kwargs["Key"] == "tenants/acme/doc-abc/v2"
        assert result.version == 2
        assert result.storage_uri == "s3://bkt/tenants/acme/doc-abc/v2"


# ---------------------------------------------------------------------------
# download — request construction + bytes parsing
# ---------------------------------------------------------------------------


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_latest_reads_streaming_body(self) -> None:
        payload = b"PDF-BYTES"
        client = _FakeS3Client(get_payload=payload)
        adapter = AwsS3StorageAdapter("docs", client=client)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="s3://docs/doc-abc/v1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="s3://docs/doc-abc/v2"),
            ]
        )

        body = await adapter.download(document)

        # (a) outbound request targets the *latest* version key.
        method, kwargs = client.calls[0]
        assert method == "get_object"
        assert kwargs == {"Bucket": "docs", "Key": "doc-abc/v2"}

        # (b) StreamingBody was consumed into raw bytes.
        assert body == payload
        assert isinstance(body, bytes)

    @pytest.mark.asyncio
    async def test_download_explicit_version_targets_that_key(self) -> None:
        client = _FakeS3Client(get_payload=b"v1-bytes")
        adapter = AwsS3StorageAdapter("docs", client=client)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="s3://docs/doc-abc/v1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="s3://docs/doc-abc/v2"),
            ]
        )

        body = await adapter.download(document, version=1)

        _method, kwargs = client.calls[0]
        assert kwargs["Key"] == "doc-abc/v1"
        assert body == b"v1-bytes"

    @pytest.mark.asyncio
    async def test_download_without_versions_raises_and_makes_no_call(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("docs", client=client)

        with pytest.raises(FileNotFoundError):
            await adapter.download(_doc(versions=[]))

        # The adapter short-circuits before ever touching S3.
        assert client.calls == []


# ---------------------------------------------------------------------------
# delete — request construction, bool result, and error-path mapping
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_all_versions_issues_one_call_each(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("docs", client=client)
        document = _doc(
            versions=[
                DocumentVersion(version=1, content_hash="a", size_bytes=1, storage_uri="s3://docs/doc-abc/v1"),
                DocumentVersion(version=2, content_hash="b", size_bytes=2, storage_uri="s3://docs/doc-abc/v2"),
            ]
        )

        ok = await adapter.delete(document)

        assert ok is True
        keys = [kwargs["Key"] for method, kwargs in client.calls if method == "delete_object"]
        assert keys == ["doc-abc/v1", "doc-abc/v2"]

    @pytest.mark.asyncio
    async def test_delete_single_version_targets_that_key(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("docs", client=client)

        ok = await adapter.delete(_doc(versions=[]), version=3)

        assert ok is True
        assert len(client.calls) == 1
        method, kwargs = client.calls[0]
        assert method == "delete_object"
        assert kwargs == {"Bucket": "docs", "Key": "doc-abc/v3"}

    @pytest.mark.asyncio
    async def test_delete_no_versions_returns_false_without_calling(self) -> None:
        client = _FakeS3Client()
        adapter = AwsS3StorageAdapter("docs", client=client)

        ok = await adapter.delete(_doc(versions=[]))

        assert ok is False
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_delete_maps_client_error_to_false(self) -> None:
        # Error path: a failing S3 delete_object is mapped to a False result,
        # not re-raised, when a specific version is requested.
        client = _FakeS3Client(delete_raises=RuntimeError("AccessDenied"))
        adapter = AwsS3StorageAdapter("docs", client=client)

        ok = await adapter.delete(_doc(versions=[]), version=1)

        assert ok is False
        # The call was still attempted before the failure was swallowed.
        assert client.calls[0][0] == "delete_object"
