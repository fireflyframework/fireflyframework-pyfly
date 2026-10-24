"""A deterministic two-dimensional fixture, not a semantic embedding model."""

from pyfly.agentic.vectorstores import (
    InMemoryVectorStore,
    TenantScopedVectorStore,
    VectorDocument,
)


async def build_knowledge() -> TenantScopedVectorStore:
    store = TenantScopedVectorStore(InMemoryVectorStore())
    await store.initialise()
    await store.upsert(
        [
            VectorDocument(
                id="lumen-review-v1",
                text="Review requires a human decision.",
                embedding=[1.0, 0.0],
                metadata={"revision": "1", "source": "fictional-handbook"},
            )
        ],
        tenant_id="lumen",
        workspace_id="help",
    )
    return store
