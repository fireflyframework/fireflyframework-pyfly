# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly Enterprise Content Management — documents, folders, e-signature."""

from __future__ import annotations

from pyfly.ecm.adapters.adobe_sign import AdobeSignESignatureAdapter
from pyfly.ecm.adapters.aws_s3 import AwsS3StorageAdapter
from pyfly.ecm.adapters.azure_blob import AzureBlobStorageAdapter
from pyfly.ecm.adapters.docusign import DocuSignESignatureAdapter
from pyfly.ecm.adapters.local_filesystem import LocalFilesystemStorageAdapter
from pyfly.ecm.adapters.logalty import LogaltyESignatureAdapter
from pyfly.ecm.adapters.noop_esignature import NoOpESignatureAdapter
from pyfly.ecm.models import (
    Document,
    DocumentVersion,
    ESignatureEnvelope,
    ESignatureStatus,
    Folder,
    Recipient,
    SignatureRequest,
)
from pyfly.ecm.ports import (
    DocumentStoragePort,
    ESignatureAdapter,
    FolderRepositoryPort,
    MetadataStoragePort,
)
from pyfly.ecm.services import DocumentService, ESignatureService

__all__ = [
    "AdobeSignESignatureAdapter",
    "AwsS3StorageAdapter",
    "AzureBlobStorageAdapter",
    "DocuSignESignatureAdapter",
    "Document",
    "DocumentService",
    "DocumentStoragePort",
    "DocumentVersion",
    "ESignatureAdapter",
    "ESignatureEnvelope",
    "ESignatureService",
    "ESignatureStatus",
    "Folder",
    "FolderRepositoryPort",
    "LocalFilesystemStorageAdapter",
    "LogaltyESignatureAdapter",
    "MetadataStoragePort",
    "NoOpESignatureAdapter",
    "Recipient",
    "SignatureRequest",
]
