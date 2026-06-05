# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Regression tests for ECM fixes.

#120 — storage + e-signature adapters selected from provider config.
#125 — DocumentService.delete honors the storage delete result.
"""

from __future__ import annotations

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.ecm.adapters.adobe_sign import AdobeSignESignatureAdapter
from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter
from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter
from pyfly.ecm.adapters.docusign import DocuSignESignatureAdapter
from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter
from pyfly.ecm.adapters.logalty import LogaltyESignatureAdapter
from pyfly.ecm.adapters.noop_esignature import NoOpESignatureAdapter
from pyfly.ecm.auto_configuration import EcmAutoConfiguration
from pyfly.ecm.models import Document
from pyfly.ecm.services import DocumentService, ESignatureService

# ---------------------------------------------------------------------------
# #120 — provider selection
# ---------------------------------------------------------------------------


class TestStorageProviderSelection:
    def test_default_is_local(self):
        cfg = Config({})
        adapter = EcmAutoConfiguration().document_storage(cfg)
        assert isinstance(adapter, LocalFilesystemStorageAdapter)

    def test_s3_provider(self):
        cfg = Config({"pyfly": {"ecm": {"storage": {"provider": "s3", "s3": {"bucket": "b", "region": "eu"}}}}})
        adapter = EcmAutoConfiguration().document_storage(cfg)
        assert isinstance(adapter, AwsS3StorageAdapter)

    def test_azure_provider(self):
        cfg = Config({"pyfly": {"ecm": {"storage": {"provider": "azure", "azure": {"container": "c"}}}}})
        adapter = EcmAutoConfiguration().document_storage(cfg)
        assert isinstance(adapter, AzureBlobStorageAdapter)


class TestESignatureProviderSelection:
    def test_default_is_noop(self):
        adapter = EcmAutoConfiguration().esignature_adapter(Config({}))
        assert isinstance(adapter, NoOpESignatureAdapter)

    def test_docusign_provider(self):
        cfg = Config(
            {
                "pyfly": {
                    "ecm": {
                        "esignature": {
                            "provider": "docusign",
                            "docusign": {"base-url": "https://x", "account-id": "a", "access-token": "t"},
                        }
                    }
                }
            }
        )
        adapter = EcmAutoConfiguration().esignature_adapter(cfg)
        assert isinstance(adapter, DocuSignESignatureAdapter)

    def test_adobe_provider(self):
        cfg = Config({"pyfly": {"ecm": {"esignature": {"provider": "adobe"}}}})
        adapter = EcmAutoConfiguration().esignature_adapter(cfg)
        assert isinstance(adapter, AdobeSignESignatureAdapter)

    def test_logalty_provider(self):
        cfg = Config({"pyfly": {"ecm": {"esignature": {"provider": "logalty"}}}})
        adapter = EcmAutoConfiguration().esignature_adapter(cfg)
        assert isinstance(adapter, LogaltyESignatureAdapter)


# ---------------------------------------------------------------------------
# #125 — delete honors storage result
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, delete_ok: bool) -> None:
        self._ok = delete_ok

    async def upload(self, document, content):  # pragma: no cover - unused here
        raise NotImplementedError

    async def download(self, document, version=None):  # pragma: no cover - unused here
        raise NotImplementedError

    async def delete(self, document, version=None) -> bool:
        return self._ok


class _FakeMetadata:
    def __init__(self, document, delete_ok: bool) -> None:
        self._doc = document
        self._ok = delete_ok

    async def save(self, document):  # pragma: no cover - unused here
        return document

    async def get(self, document_id):
        return self._doc

    async def list(self, folder_id=None):  # pragma: no cover - unused here
        return []

    async def delete(self, document_id) -> bool:
        return self._ok


def _doc() -> Document:
    return Document(name="f.txt", content_type="text/plain", size_bytes=0)


class TestDeleteHonorsStorageResult:
    @pytest.mark.asyncio
    async def test_both_succeed_returns_true(self):
        doc = _doc()
        svc = DocumentService(storage=_FakeStorage(True), metadata=_FakeMetadata(doc, True))
        assert await svc.delete(doc.id) is True

    @pytest.mark.asyncio
    async def test_storage_failure_surfaces_false(self):
        doc = _doc()
        svc = DocumentService(storage=_FakeStorage(False), metadata=_FakeMetadata(doc, True))
        assert await svc.delete(doc.id) is False

    @pytest.mark.asyncio
    async def test_missing_document_returns_false(self):
        svc = DocumentService(storage=_FakeStorage(True), metadata=_FakeMetadata(None, True))
        assert await svc.delete("nope") is False


# ---------------------------------------------------------------------------
# #120 — port-typed beans wire through a real ApplicationContext
# ---------------------------------------------------------------------------


class TestEcmContextWiring:
    @pytest.mark.asyncio
    async def test_context_resolves_ecm_services(self):
        ctx = ApplicationContext(Config({"pyfly": {"ecm": {"enabled": "true"}}}))
        await ctx.start()
        try:
            doc_svc = ctx.get_bean(DocumentService)
            esig_svc = ctx.get_bean(ESignatureService)
            assert isinstance(doc_svc, DocumentService)
            assert isinstance(esig_svc, ESignatureService)
            # The document round-trips through the auto-wired storage + metadata ports.
            uploaded = await doc_svc.upload(name="hello.txt", content=b"hi")
            assert await doc_svc.download(uploaded.id) == b"hi"
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_context_skips_ecm_when_disabled(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        try:
            with pytest.raises(Exception):  # noqa: B017,PT011 - no ECM bean registered
                ctx.get_bean(DocumentService)
        finally:
            await ctx.stop()
