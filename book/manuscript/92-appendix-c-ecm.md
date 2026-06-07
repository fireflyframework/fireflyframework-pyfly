<span class="eyebrow">Appendix C</span>

# ECM: Content & E-Signature {.chtitle}

`pyfly.ecm` provides hexagonal abstractions for enterprise document management:
blob storage, document metadata, folder hierarchies, and e-signature workflows.
The module ships fully wired adapters for local storage, AWS S3, and Azure Blob
Storage, plus e-signature adapters for DocuSign, Adobe Sign, and Logalty — all
selectable without changing a line of application code, purely via YAML.

!!! note "No extra dependency for the module itself"
    The core ECM module (`pyfly.ecm`) has no third-party dependencies beyond
    the standard library. Cloud storage adapters (`boto3`, `azure-storage-blob`)
    and e-signature SDK calls are made through lightweight async wrappers in
    each adapter class; install only what you need.

---

## Domain model

All ECM types are plain Python dataclasses defined in `src/pyfly/ecm/models.py`.

| Class | Key fields | Description |
|---|---|---|
| `Document` | `id`, `name`, `folder_id`, `content_type`, `size_bytes`, `metadata`, `versions` | Logical document record |
| `DocumentVersion` | `version`, `content_hash`, `size_bytes`, `storage_uri` | Immutable version snapshot |
| `Folder` | `id`, `name`, `parent_id`, `path` | Folder node in the hierarchy |
| `Recipient` | `name`, `email`, `role` | E-signature recipient |
| `SignatureRequest` | `document_id`, `recipients`, `subject`, `message` | Signature request payload |
| `ESignatureEnvelope` | `id`, `provider`, `status`, `provider_envelope_id` | Envelope tracking record |

`ESignatureStatus` is a `StrEnum` with values `DRAFT`, `SENT`, `SIGNED`,
`DECLINED`, and `EXPIRED`.

---

## Ports (contracts)

Four `Protocol` classes defined in `src/pyfly/ecm/ports.py` form the hexagonal
boundary. Your application services depend on these contracts; adapters implement
them.

### DocumentStoragePort

```python
class DocumentStoragePort(Protocol):
    name: str
    async def upload(
        self, document: Document, content: bytes
    ) -> DocumentVersion: ...
    async def download(
        self, document: Document, version: int | None = None
    ) -> bytes: ...
    async def delete(
        self, document: Document, version: int | None = None
    ) -> bool: ...
```

### MetadataStoragePort

```python
class MetadataStoragePort(Protocol):
    async def save(self, document: Document) -> Document: ...
    async def get(self, document_id: str) -> Document | None: ...
    async def list(
        self, folder_id: str | None = None, *, limit: int = 100
    ) -> list[Document]: ...
    async def delete(self, document_id: str) -> bool: ...
```

### FolderRepositoryPort

```python
class FolderRepositoryPort(Protocol):
    async def save(self, folder: Folder) -> Folder: ...
    async def get(self, folder_id: str) -> Folder | None: ...
    async def list(self, parent_id: str | None = None) -> list[Folder]: ...
    async def delete(self, folder_id: str) -> bool: ...
```

### ESignatureAdapter

```python
class ESignatureAdapter(Protocol):
    name: str
    async def send(
        self, request: SignatureRequest
    ) -> ESignatureEnvelope: ...
    async def get(self, envelope_id: str) -> ESignatureEnvelope | None: ...
    async def cancel(self, envelope_id: str) -> bool: ...
```

---

## DocumentService

`DocumentService` orchestrates the storage and metadata ports. It is the primary
entry point for document upload, download, listing, and deletion.

::: listing contracts/ecm_usage.py | Listing C.1 — DocumentService: upload, download, delete
from pyfly.ecm import (
    DocumentService,
    LocalFilesystemStorageAdapter,
)
from pyfly.ecm.in_memory import (
    InMemoryFolderRepository,
    InMemoryMetadataStorage,
)


service = DocumentService(
    storage=LocalFilesystemStorageAdapter("/var/ecm/docs"),
    metadata=InMemoryMetadataStorage(),
    folders=InMemoryFolderRepository(),
)


async def demo() -> None:
    # Upload
    doc = await service.upload(
        name="contract.pdf",
        content=b"%PDF-1.4 ...",
        content_type="application/pdf",
        metadata={"department": "legal", "year": "2026"},
    )

    # Download (latest version)
    content = await service.download(doc.id)

    # Download a specific version
    v1_content = await service.download(doc.id, version=1)

    # List documents in a folder
    docs = await service.list(folder_id=doc.folder_id)

    # Delete (returns False if blob delete partially fails)
    ok = await service.delete(doc.id)
:::

### `delete` semantics

`DocumentService.delete(document_id)` removes both the stored blob and the metadata
record and returns the **logical AND** of the two delete results. If the document ID
is unknown it returns `False` immediately. If the blob delete fails the metadata
record is still removed (so the logical document is gone), but the method returns
`False` to signal a partial delete. This contract is verified in the source at
`src/pyfly/ecm/services.py`.

### Folder management

::: listing contracts/ecm_folders.py | Listing C.2 — Creating folder hierarchies
from pyfly.ecm import DocumentService, Folder


async def setup_folders(service: DocumentService) -> None:
    root = await service.create_folder(
        Folder(name="contracts", path="/contracts")
    )
    legal = await service.create_folder(
        Folder(name="legal", parent_id=root.id, path="/contracts/legal")
    )
    doc = await service.upload(
        name="nda.pdf",
        content=b"...",
        folder_id=legal.id,
        content_type="application/pdf",
    )
:::

!!! note "Folder port is optional"
    Pass `folders=None` to `DocumentService` if you do not need folder support.
    Calling `create_folder` on such an instance raises `RuntimeError`.

---

## ESignatureService

`ESignatureService` wraps an `ESignatureAdapter` and exposes three operations:
`request`, `get`, and `cancel`.

::: listing contracts/esignature_usage.py | Listing C.3 — Requesting an e-signature
from pyfly.ecm import (
    ESignatureService,
    NoOpESignatureAdapter,
    Recipient,
    SignatureRequest,
)


esig = ESignatureService(adapter=NoOpESignatureAdapter())


async def send_for_signature(document_id: str) -> str:
    envelope = await esig.request(
        SignatureRequest(
            document_id=document_id,
            recipients=[
                Recipient(
                    name="Alice Johnson",
                    email="alice@example.com",
                    role="signer",
                ),
            ],
            subject="Please sign your employment contract",
            message="Review and sign at your earliest convenience.",
        )
    )
    return envelope.id
:::

---

## Storage adapters

| Adapter class | `pyfly.ecm.storage.provider` value | Notes |
|---|---|---|
| `LocalFilesystemStorageAdapter` | `local` (default) | Stores blobs as `<base_dir>/<id>/v<n>` |
| `AwsS3StorageAdapter` | `s3` or `aws` | Requires `boto3`; async via `run_in_executor` |
| `AzureBlobStorageAdapter` | `azure` | Requires `azure-storage-blob` |

All three implement `DocumentStoragePort`. `LocalFilesystemStorageAdapter` creates
the base directory on construction and stores each version as a separate file, making
it suitable for development and testing without external services.

## E-signature adapters

| Adapter class | `pyfly.ecm.esignature.provider` value | Notes |
|---|---|---|
| `NoOpESignatureAdapter` | `noop` (default) | Returns stub envelopes; safe for dev/test |
| `DocuSignESignatureAdapter` | `docusign` | REST API calls via `httpx` |
| `AdobeSignESignatureAdapter` | `adobe` | REST API calls via `httpx` |
| `LogaltyESignatureAdapter` | `logalty` | REST API calls via `httpx` |

Implement `ESignatureAdapter` to add a custom provider (e.g. an in-house signing
service). Register your implementation as a bean and inject it into `ESignatureService`.

---

## Auto-configuration

`EcmAutoConfiguration` activates when `pyfly.ecm.enabled=true` is set in config.
It registers five beans:

| Bean | Default implementation |
|---|---|
| `metadata_storage` | `InMemoryMetadataStorage` |
| `folder_repository` | `InMemoryFolderRepository` |
| `document_storage` | `LocalFilesystemStorageAdapter` (temp dir) |
| `document_service` | `DocumentService` wiring all three above |
| `esignature_adapter` | `NoOpESignatureAdapter` |
| `esignature_service` | `ESignatureService` wrapping the adapter |

The storage and e-signature adapters are selected from config so you switch providers
purely via YAML — no code change required:

::: listing pyfly.yaml | Listing C.4 — Full ECM YAML with S3 storage and DocuSign
pyfly:
  ecm:
    enabled: true
    storage:
      provider: s3
      local:
        base-dir: /var/ecm
      s3:
        bucket: my-docs-bucket
        region: eu-west-1
        key-prefix: documents/
      azure:
        container: my-container
        connection-string: "DefaultEndpointsProtocol=https;..."
        account-url: "https://acct.blob.core.windows.net"
        key-prefix: documents/
    esignature:
      provider: docusign
      docusign:
        base-url: "https://demo.docusign.net/restapi"
        account-id: "your-account-id"
        access-token: "your-access-token"
      adobe:
        api-base: "https://api.na4.adobesign.com"
        access-token: "your-token"
      logalty:
        api-base: "https://api.logalty.com"
        api-key: "your-api-key"
:::

!!! tip "Start with local + noop"
    In development, omit the `provider` keys and let the defaults take effect:
    `local` storage uses a fresh temp directory and `noop` e-signature returns
    deterministic stub data. No external services needed.

---

## Implementing a custom storage adapter

Implement `DocumentStoragePort` and register it as a `@bean`:

::: listing infra/gcs_storage.py | Listing C.5 — Custom storage adapter skeleton
from pyfly.ecm.models import Document, DocumentVersion
from pyfly.ecm.ports import DocumentStoragePort


class GcsStorageAdapter:
    """Google Cloud Storage adapter (skeleton)."""

    name = "gcs"

    def __init__(self, bucket: str, key_prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = key_prefix

    async def upload(
        self, document: Document, content: bytes
    ) -> DocumentVersion:
        version_no = (
            (document.versions[-1].version + 1) if document.versions else 1
        )
        key = f"{self._prefix}{document.id}/v{version_no}"
        # ... upload content to GCS ...
        return DocumentVersion(
            version=version_no,
            content_hash="sha256-placeholder",
            size_bytes=len(content),
            storage_uri=f"gs://{self._bucket}/{key}",
        )

    async def download(
        self, document: Document, version: int | None = None
    ) -> bytes:
        target = version or document.versions[-1].version
        key = f"{self._prefix}{document.id}/v{target}"
        # ... download from GCS ...
        return b""

    async def delete(
        self, document: Document, version: int | None = None
    ) -> bool:
        # ... delete object(s) from GCS ...
        return True
:::

Source files:
- `src/pyfly/ecm/models.py` — domain dataclasses
- `src/pyfly/ecm/ports.py` — Protocol contracts
- `src/pyfly/ecm/services.py` — `DocumentService`, `ESignatureService`
- `src/pyfly/ecm/adapters/` — bundled storage and e-signature adapters
- `src/pyfly/ecm/in_memory.py` — `InMemoryMetadataStorage`, `InMemoryFolderRepository`
- `src/pyfly/ecm/auto_configuration.py` — `EcmAutoConfiguration`
