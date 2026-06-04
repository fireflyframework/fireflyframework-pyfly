# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the ECM module.

Storage and e-signature adapters are selected from configuration so a user can
switch providers purely via YAML, mirroring Spring's ECM starter:

    pyfly:
      ecm:
        enabled: true
        storage:
          provider: s3            # local (default) | s3/aws | azure
          local:
            base-dir: /var/ecm    # default: a fresh temp directory
          s3:
            bucket: my-bucket
            region: eu-west-1
            key-prefix: documents/
          azure:
            container: my-container
            connection-string: ...
            account-url: https://acct.blob.core.windows.net
        esignature:
          provider: docusign      # noop (default) | docusign | adobe | logalty
          docusign:
            base-url: https://demo.docusign.net/restapi
            account-id: ...
            access-token: ...
"""

from __future__ import annotations

import tempfile

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter
from pyfly.ecm.adapters.noop_esignature import NoOpESignatureAdapter
from pyfly.ecm.in_memory import InMemoryFolderRepository, InMemoryMetadataStorage
from pyfly.ecm.ports import (
    DocumentStoragePort,
    ESignatureAdapter,
    FolderRepositoryPort,
    MetadataStoragePort,
)
from pyfly.ecm.services import DocumentService, ESignatureService


@auto_configuration
@conditional_on_property("pyfly.ecm.enabled", having_value="true")
class EcmAutoConfiguration:
    @bean
    def metadata_storage(self) -> MetadataStoragePort:
        return InMemoryMetadataStorage()

    @bean
    def folder_repository(self) -> FolderRepositoryPort:
        return InMemoryFolderRepository()

    @bean
    def document_storage(self, config: Config) -> DocumentStoragePort:
        """Select the storage adapter from ``pyfly.ecm.storage.provider`` (audit #120)."""
        provider = str(config.get("pyfly.ecm.storage.provider", "local")).lower()

        if provider in ("s3", "aws"):
            from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter

            return AwsS3StorageAdapter(
                bucket=str(config.get("pyfly.ecm.storage.s3.bucket", "")),
                region=config.get("pyfly.ecm.storage.s3.region"),
                key_prefix=str(config.get("pyfly.ecm.storage.s3.key-prefix", "") or ""),
            )

        if provider in ("azure", "azure-blob", "azure_blob"):
            from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter

            return AzureBlobStorageAdapter(
                container=str(config.get("pyfly.ecm.storage.azure.container", "")),
                connection_string=config.get("pyfly.ecm.storage.azure.connection-string"),
                account_url=config.get("pyfly.ecm.storage.azure.account-url"),
                key_prefix=str(config.get("pyfly.ecm.storage.azure.key-prefix", "") or ""),
            )

        base_dir = config.get("pyfly.ecm.storage.local.base-dir") or tempfile.mkdtemp(prefix="pyfly-ecm-")
        return LocalFilesystemStorageAdapter(base_dir)

    @bean
    def document_service(
        self,
        storage: DocumentStoragePort,
        metadata: MetadataStoragePort,
        folders: FolderRepositoryPort,
    ) -> DocumentService:
        return DocumentService(storage=storage, metadata=metadata, folders=folders)

    @bean
    def esignature_adapter(self, config: Config) -> ESignatureAdapter:
        """Select the e-signature adapter from ``pyfly.ecm.esignature.provider`` (audit #120)."""
        provider = str(config.get("pyfly.ecm.esignature.provider", "noop")).lower()

        if provider == "docusign":
            from pyfly.ecm.adapters.docusign import DocuSignESignatureAdapter

            return DocuSignESignatureAdapter(
                base_url=str(config.get("pyfly.ecm.esignature.docusign.base-url", "")),
                account_id=str(config.get("pyfly.ecm.esignature.docusign.account-id", "")),
                access_token=str(config.get("pyfly.ecm.esignature.docusign.access-token", "")),
            )

        if provider in ("adobe", "adobe-sign", "adobe_sign"):
            from pyfly.ecm.adapters.adobe_sign import AdobeSignESignatureAdapter

            return AdobeSignESignatureAdapter(
                api_base=str(config.get("pyfly.ecm.esignature.adobe.api-base", "")),
                access_token=str(config.get("pyfly.ecm.esignature.adobe.access-token", "")),
            )

        if provider == "logalty":
            from pyfly.ecm.adapters.logalty import LogaltyESignatureAdapter

            return LogaltyESignatureAdapter(
                api_base=str(config.get("pyfly.ecm.esignature.logalty.api-base", "")),
                api_key=str(config.get("pyfly.ecm.esignature.logalty.api-key", "")),
            )

        return NoOpESignatureAdapter()

    @bean
    def esignature_service(self, adapter: ESignatureAdapter) -> ESignatureService:
        return ESignatureService(adapter=adapter)
