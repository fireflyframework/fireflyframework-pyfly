# Enterprise Content Management (ECM)

`pyfly.ecm` provides hexagonal abstractions for document storage,
metadata, folders and e-signature workflows.

## Document upload / download

```python
from pyfly.ecm import (
    DocumentService, LocalFilesystemStorageAdapter,
)
from pyfly.ecm.in_memory import InMemoryFolderRepository, InMemoryMetadataStorage

service = DocumentService(
    storage=LocalFilesystemStorageAdapter("/var/firefly/docs"),
    metadata=InMemoryMetadataStorage(),
    folders=InMemoryFolderRepository(),
)

doc = await service.upload(name="contract.pdf", content=pdf_bytes, content_type="application/pdf")
content = await service.download(doc.id)
```

## E-signature

```python
from pyfly.ecm import (
    ESignatureService, NoOpESignatureAdapter, Recipient, SignatureRequest,
)

service = ESignatureService(adapter=NoOpESignatureAdapter())
envelope = await service.request(
    SignatureRequest(
        document_id=doc.id,
        recipients=[Recipient(name="Alice", email="alice@example.com")],
        subject="Please sign your contract",
    ),
)
```

Replace ``NoOpESignatureAdapter`` with a real provider (DocuSign, Adobe
Sign, Logalty) by implementing the ``ESignatureAdapter`` protocol -- or, when
running under auto-configuration, just select one in YAML (see below). The
DocuSign, Adobe Sign and Logalty adapters ship with the module
(``pyfly.ecm.DocuSignESignatureAdapter`` / ``AdobeSignESignatureAdapter`` /
``LogaltyESignatureAdapter``).

## `DocumentService.delete`

`delete(document_id)` removes both the stored blob and the metadata record and
returns the **logical AND** of the two delete results. A failed blob delete is
no longer silently swallowed: the metadata record is still removed (so the
logical document is gone), but the method returns `False` to tell the caller
the delete was only partial. It returns `False` immediately if the document id
is unknown.

## Auto-configuration & provider selection

`EcmAutoConfiguration` activates when `pyfly.ecm.enabled=true`. The storage and
e-signature adapters are **selected from configuration**, so you switch
providers purely via YAML (mirroring Spring's ECM starter). Previously only the
local filesystem storage and no-op e-signature adapters were ever wired,
regardless of config.

```yaml
pyfly:
  ecm:
    enabled: true
    storage:
      provider: s3            # local (default) | s3 / aws | azure
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
        key-prefix: documents/
    esignature:
      provider: docusign      # noop (default) | docusign | adobe | logalty
      docusign:
        base-url: https://demo.docusign.net/restapi
        account-id: ...
        access-token: ...
      adobe:
        api-base: ...
        access-token: ...
      logalty:
        api-base: ...
        api-key: ...
```

**Storage provider** (`pyfly.ecm.storage.provider`):

| Value | Adapter |
|-------|---------|
| `local` (default) | `LocalFilesystemStorageAdapter` (uses `storage.local.base-dir`, or a fresh temp directory) |
| `s3` / `aws` | `AwsS3StorageAdapter` (`s3.bucket`, `s3.region`, `s3.key-prefix`) |
| `azure` | `AzureBlobStorageAdapter` (`azure.container`, `azure.connection-string`, `azure.account-url`, `azure.key-prefix`) |

**E-signature provider** (`pyfly.ecm.esignature.provider`):

| Value | Adapter |
|-------|---------|
| `noop` (default) | `NoOpESignatureAdapter` |
| `docusign` | `DocuSignESignatureAdapter` (`docusign.base-url`, `.account-id`, `.access-token`) |
| `adobe` | `AdobeSignESignatureAdapter` (`adobe.api-base`, `.access-token`) |
| `logalty` | `LogaltyESignatureAdapter` (`logalty.api-base`, `.api-key`) |

Metadata and folder repositories default to the in-memory implementations
(`InMemoryMetadataStorage` / `InMemoryFolderRepository`).
