# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the ECM module."""

from __future__ import annotations

import tempfile

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter
from pyfly.ecm.adapters.noop_esignature import NoOpESignatureAdapter
from pyfly.ecm.in_memory import InMemoryFolderRepository, InMemoryMetadataStorage
from pyfly.ecm.services import DocumentService, ESignatureService


@auto_configuration
@conditional_on_property("pyfly.ecm.enabled", having_value="true")
class EcmAutoConfiguration:
    @bean
    def metadata_storage(self) -> InMemoryMetadataStorage:
        return InMemoryMetadataStorage()

    @bean
    def folder_repository(self) -> InMemoryFolderRepository:
        return InMemoryFolderRepository()

    @bean
    def document_storage(self) -> LocalFilesystemStorageAdapter:
        return LocalFilesystemStorageAdapter(tempfile.mkdtemp(prefix="pyfly-ecm-"))

    @bean
    def document_service(
        self,
        storage: LocalFilesystemStorageAdapter,
        metadata: InMemoryMetadataStorage,
        folders: InMemoryFolderRepository,
    ) -> DocumentService:
        return DocumentService(storage=storage, metadata=metadata, folders=folders)

    @bean
    def esignature_adapter(self) -> NoOpESignatureAdapter:
        return NoOpESignatureAdapter()

    @bean
    def esignature_service(self, adapter: NoOpESignatureAdapter) -> ESignatureService:
        return ESignatureService(adapter=adapter)
