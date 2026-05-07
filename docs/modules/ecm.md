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
Sign, Logalty) by implementing the ``ESignatureAdapter`` protocol.
