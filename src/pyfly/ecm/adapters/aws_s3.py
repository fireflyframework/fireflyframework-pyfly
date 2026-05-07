# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""AWS S3 document storage adapter — wraps boto3's ``s3`` client."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from pyfly.ecm.models import Document, DocumentVersion


class AwsS3StorageAdapter:
    """Stores document blobs in an S3 bucket under ``<key_prefix>/<document_id>/v<version>``."""

    name = "aws-s3"

    def __init__(
        self,
        bucket: str,
        *,
        region: str | None = None,
        key_prefix: str = "",
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._prefix = key_prefix.rstrip("/") + "/" if key_prefix else ""
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # noqa: BLE001
            msg = "AwsS3StorageAdapter requires boto3 — `pip install boto3`"
            raise ImportError(msg) from exc
        self._client = boto3.client("s3", region_name=self._region) if self._region else boto3.client("s3")
        return self._client

    async def _run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _key(self, document_id: str, version: int) -> str:
        return f"{self._prefix}{document_id}/v{version}"

    async def upload(self, document: Document, content: bytes) -> DocumentVersion:
        client = self._ensure_client()
        version_no = (document.versions[-1].version + 1) if document.versions else 1
        key = self._key(document.id, version_no)
        await self._run(
            client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType=document.content_type,
        )
        return DocumentVersion(
            version=version_no,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            storage_uri=f"s3://{self._bucket}/{key}",
        )

    async def download(self, document: Document, version: int | None = None) -> bytes:
        if not document.versions:
            msg = f"document '{document.id}' has no versions"
            raise FileNotFoundError(msg)
        target_version = version or document.versions[-1].version
        client = self._ensure_client()
        resp = await self._run(client.get_object, Bucket=self._bucket, Key=self._key(document.id, target_version))
        body = resp["Body"].read() if hasattr(resp["Body"], "read") else resp["Body"]
        return body if isinstance(body, bytes) else bytes(body)

    async def delete(self, document: Document, version: int | None = None) -> bool:
        client = self._ensure_client()
        if version is None:
            for v in document.versions:
                await self._run(client.delete_object, Bucket=self._bucket, Key=self._key(document.id, v.version))
            return bool(document.versions)
        try:
            await self._run(client.delete_object, Bucket=self._bucket, Key=self._key(document.id, version))
            return True
        except Exception:  # noqa: BLE001
            return False
