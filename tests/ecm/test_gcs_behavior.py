# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native GCS requests and document-version behavior without cloud access."""

from __future__ import annotations

import hashlib

import pytest

from pyfly.core.config import Config
from pyfly.ecm.auto_configuration import EcmAutoConfiguration
from pyfly.ecm.models import Document


def adapter_type():
    from pyfly.ecm.adapters.gcs import GoogleCloudStorageAdapter

    return GoogleCloudStorageAdapter


class Blob:
    def __init__(self, client, key):
        self.client, self.key = client, key

    def upload_from_string(self, data, **kwargs):
        self.client.calls.append(("upload", self.key, kwargs))
        if kwargs["if_generation_match"] != 0 or self.key in self.client.objects:
            raise RuntimeError("generation precondition failed")
        if self.client.fail:
            raise TimeoutError("response lost")
        self.client.objects[self.key] = data

    def download_as_bytes(self, **kwargs):
        self.client.calls.append(("download", self.key, kwargs))
        return self.client.objects[self.key]

    def delete(self, **kwargs):
        self.client.calls.append(("delete", self.key, kwargs))
        if self.key not in self.client.objects:
            raise FileNotFoundError(self.key)
        del self.client.objects[self.key]


class Client:
    def __init__(self):
        self.objects, self.calls, self.buckets = {}, [], []
        self.fail = False

    def bucket(self, name):
        self.buckets.append(name)
        return self

    def blob(self, key):
        return Blob(self, key)


@pytest.mark.asyncio
async def test_upload_is_versioned_create_only_and_returns_actual_sha256():
    client = Client()
    adapter = adapter_type()("artifacts", key_prefix="tenants/acme/", client=client)
    document = Document(id="source", content_type="application/x-tar")
    result = await adapter.upload(document, b"source bytes")
    assert client.calls == [
        (
            "upload",
            "tenants/acme/source/v1",
            {
                "content_type": "application/x-tar",
                "if_generation_match": 0,
                "retry": None,
            },
        )
    ]
    assert result.version == 1 and result.size_bytes == 12
    assert result.content_hash == hashlib.sha256(b"source bytes").hexdigest()
    assert result.storage_uri == "gs://artifacts/tenants/acme/source/v1"
    document.versions.append(result)
    next_version = await adapter.upload(document, b"next")
    assert next_version.version == 2 and next_version.storage_uri.endswith("/v2")
    assert await adapter.download(document) == b"source bytes"


@pytest.mark.asyncio
async def test_duplicate_version_and_unknown_upload_are_not_overwritten_or_retried():
    client = Client()
    adapter = adapter_type()("artifacts", client=client)
    document = Document(id="source")
    await adapter.upload(document, b"original")
    with pytest.raises(RuntimeError, match="precondition"):
        await adapter.upload(document, b"changed")
    assert client.objects["source/v1"] == b"original" and len(client.calls) == 2
    client.fail = True
    with pytest.raises(TimeoutError):
        await adapter.upload(Document(id="uncertain"), b"payload")
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_download_and_delete_resolve_only_retained_document_versions():
    client = Client()
    adapter = adapter_type()("artifacts", client=client)
    document = Document(id="source")
    one = await adapter.upload(document, b"one")
    document.versions.append(one)
    two = await adapter.upload(document, b"two")
    document.versions.append(two)
    assert await adapter.download(document, 1) == b"one"
    assert await adapter.download(document) == b"two"
    with pytest.raises(FileNotFoundError):
        await adapter.download(document, 3)
    assert await adapter.delete(document, 1) is True
    assert await adapter.delete(document, 1) is False
    assert await adapter.delete(document) is True
    assert client.objects == {}
    assert all(kwargs.get("retry") is None for _, _, kwargs in client.calls)


@pytest.mark.asyncio
async def test_empty_document_download_refuses_before_accessing_client():
    client = Client()
    adapter = adapter_type()("artifacts", client=client)
    with pytest.raises(FileNotFoundError):
        await adapter.download(Document())
    assert client.calls == [] and client.buckets == []


@pytest.mark.parametrize("provider", ["gcs", "google-cloud-storage"])
def test_configuration_selects_native_gcs_without_local_fallback(provider):
    configuration = Config(
        {
            "pyfly": {
                "ecm": {
                    "storage": {
                        "provider": provider,
                        "gcs": {"bucket": "artifacts", "project": "application", "key-prefix": "documents/"},
                    }
                }
            }
        }
    )
    adapter = EcmAutoConfiguration().document_storage(configuration)
    assert isinstance(adapter, adapter_type())
    assert adapter._bucket == "artifacts" and adapter._project == "application"


def test_provider_requires_bucket_before_any_client_or_credentials_lookup():
    with pytest.raises(ValueError, match="bucket"):
        adapter_type()("")
