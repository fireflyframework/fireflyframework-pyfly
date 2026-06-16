<span class="eyebrow">Apéndice C</span>

# ECM: contenido y firma electrónica {.chtitle}

`pyfly.ecm` proporciona abstracciones hexagonales para la gestión documental
empresarial: almacenamiento de blobs, metadatos de documentos, jerarquías de
carpetas y flujos de firma electrónica. El módulo incluye adaptadores
completamente cableados para almacenamiento local, AWS S3 y Azure Blob Storage,
además de adaptadores de firma electrónica para DocuSign, Adobe Sign y Logalty
— todos seleccionables sin cambiar una sola línea del código de la aplicación,
puramente mediante YAML.

!!! note "El módulo en sí no requiere dependencias adicionales"
    El módulo ECM central (`pyfly.ecm`) no tiene dependencias de terceros más
    allá de la biblioteca estándar. Los adaptadores de almacenamiento en la nube
    (`boto3`, `azure-storage-blob`) y las llamadas a los SDK de firma electrónica
    se realizan a través de envoltorios asíncronos ligeros en cada clase de
    adaptador; instala solo lo que necesites.

---

## Modelo de dominio

Todos los tipos de ECM son dataclasses sencillas de Python definidas en
`src/pyfly/ecm/models.py`.

| Clase | Campos clave | Descripción |
|---|---|---|
| `Document` | `id`, `name`, `folder_id`, `content_type`, `size_bytes`, `metadata`, `versions` | Registro lógico de documento |
| `DocumentVersion` | `version`, `content_hash`, `size_bytes`, `storage_uri` | Instantánea inmutable de versión |
| `Folder` | `id`, `name`, `parent_id`, `path` | Nodo de carpeta en la jerarquía |
| `Recipient` | `name`, `email`, `role` | Destinatario de firma electrónica |
| `SignatureRequest` | `document_id`, `recipients`, `subject`, `message` | Carga útil de la solicitud de firma |
| `ESignatureEnvelope` | `id`, `provider`, `status`, `provider_envelope_id` | Registro de seguimiento del sobre |

`ESignatureStatus` es un `StrEnum` con los valores `DRAFT`, `SENT`, `SIGNED`,
`DECLINED` y `EXPIRED`.

---

## Puertos (contratos)

Cuatro clases `Protocol` definidas en `src/pyfly/ecm/ports.py` forman el límite
hexagonal. Los servicios de tu aplicación dependen de estos contratos; los
adaptadores los implementan.

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

`DocumentService` orquesta los puertos de almacenamiento y de metadatos. Es el
punto de entrada principal para la subida, descarga, listado y eliminación de
documentos.

::: listing contracts/ecm_usage.py | Listado C.1 — DocumentService: subir, descargar, eliminar
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

### Semántica de `delete`

`DocumentService.delete(document_id)` elimina tanto el blob almacenado como el
registro de metadatos y devuelve el **AND lógico** de los dos resultados de
eliminación. Si el ID del documento es desconocido, devuelve `False` de
inmediato. Si la eliminación del blob falla, el registro de metadatos se elimina
igualmente (de modo que el documento lógico desaparece), pero el método devuelve
`False` para señalar una eliminación parcial. Este contrato se verifica en el
código fuente en `src/pyfly/ecm/services.py`.

### Gestión de carpetas

::: listing contracts/ecm_folders.py | Listado C.2 — Creación de jerarquías de carpetas
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

!!! note "El puerto de carpetas es opcional"
    Pasa `folders=None` a `DocumentService` si no necesitas soporte de carpetas.
    Llamar a `create_folder` en una instancia así lanza `RuntimeError`.

---

## ESignatureService

`ESignatureService` envuelve un `ESignatureAdapter` y expone tres operaciones:
`request`, `get` y `cancel`.

::: listing contracts/esignature_usage.py | Listado C.3 — Solicitar una firma electrónica
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

## Adaptadores de almacenamiento

| Clase de adaptador | Valor de `pyfly.ecm.storage.provider` | Notas |
|---|---|---|
| `LocalFilesystemStorageAdapter` | `local` (por defecto) | Almacena los blobs como `<base_dir>/<id>/v<n>` |
| `AwsS3StorageAdapter` | `s3` o `aws` | Requiere `boto3`; asíncrono mediante `run_in_executor` |
| `AzureBlobStorageAdapter` | `azure` | Requiere `azure-storage-blob` |

Los tres implementan `DocumentStoragePort`. `LocalFilesystemStorageAdapter` crea
el directorio base en el momento de la construcción y almacena cada versión como
un archivo independiente, lo que lo hace adecuado para desarrollo y pruebas sin
servicios externos.

## Adaptadores de firma electrónica

| Clase de adaptador | Valor de `pyfly.ecm.esignature.provider` | Notas |
|---|---|---|
| `NoOpESignatureAdapter` | `noop` (por defecto) | Devuelve sobres ficticios; seguro para dev/test |
| `DocuSignESignatureAdapter` | `docusign` | Llamadas a la API REST mediante `httpx` |
| `AdobeSignESignatureAdapter` | `adobe` | Llamadas a la API REST mediante `httpx` |
| `LogaltyESignatureAdapter` | `logalty` | Llamadas a la API REST mediante `httpx` |

Implementa `ESignatureAdapter` para añadir un proveedor personalizado (por
ejemplo, un servicio de firma interno). Registra tu implementación como un bean e
inyéctala en `ESignatureService`.

---

## Autoconfiguración

`EcmAutoConfiguration` se activa cuando se establece `pyfly.ecm.enabled=true` en
la configuración. Registra cinco beans:

| Bean | Implementación por defecto |
|---|---|
| `metadata_storage` | `InMemoryMetadataStorage` |
| `folder_repository` | `InMemoryFolderRepository` |
| `document_storage` | `LocalFilesystemStorageAdapter` (directorio temporal) |
| `document_service` | `DocumentService` cableando los tres anteriores |
| `esignature_adapter` | `NoOpESignatureAdapter` |
| `esignature_service` | `ESignatureService` envolviendo el adaptador |

Los adaptadores de almacenamiento y de firma electrónica se seleccionan desde la
configuración, de modo que cambias de proveedor puramente mediante YAML — sin
necesidad de cambiar código:

::: listing pyfly.yaml | Listado C.4 — YAML de ECM completo con almacenamiento S3 y DocuSign
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

!!! tip "Empieza con local + noop"
    En desarrollo, omite las claves `provider` y deja que actúen los valores por
    defecto: el almacenamiento `local` usa un directorio temporal nuevo y la
    firma electrónica `noop` devuelve datos ficticios deterministas. No se
    necesitan servicios externos.

---

## Implementar un adaptador de almacenamiento personalizado

Implementa `DocumentStoragePort` y regístralo como un `@bean`:

::: listing infra/gcs_storage.py | Listado C.5 — Esqueleto de un adaptador de almacenamiento personalizado
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

Archivos fuente:
- `src/pyfly/ecm/models.py` — dataclasses de dominio
- `src/pyfly/ecm/ports.py` — contratos Protocol
- `src/pyfly/ecm/services.py` — `DocumentService`, `ESignatureService`
- `src/pyfly/ecm/adapters/` — adaptadores de almacenamiento y firma electrónica incluidos
- `src/pyfly/ecm/in_memory.py` — `InMemoryMetadataStorage`, `InMemoryFolderRepository`
- `src/pyfly/ecm/auto_configuration.py` — `EcmAutoConfiguration`
