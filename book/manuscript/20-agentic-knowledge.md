<span class="eyebrow">Chapter 20</span>

# Knowledge and Reasoning {.chtitle}

Lumen's assistant now has a typed interface and an approval boundary. It still needs evidence. A model's training data cannot establish the current wallet review policy. We will retrieve an authorized policy revision, preserve its identity and ask for an answer constrained by that evidence. This is retrieval-augmented generation, or RAG: retrieval supplies context; generation explains it. The application remains responsible for scope, relevance and final validation.

The companion in `samples/agentic-book/src/lumen_agentic/knowledge.py` uses a real in-memory vector store wrapped by `TenantScopedVectorStore`. Its two-dimensional vectors are fixtures chosen to make ranking deterministic. They are not semantic embeddings, and the test does not measure retrieval quality. That distinction keeps offline correctness and model quality independently reviewable.

## 20.1. Prepare documents before generating answers

Start with a source record: document ID, revision, trusted tenant/workspace, media type and provenance. Load content through the appropriate adapter, normalize binary inputs and preserve the source identity through splitting. `pyfly.agentic.content` provides text chunking, Markdown-aware chunking, document splitting, image tiling, compression and batch processing. Binary and document conversion integrations have their own optional dependencies; installing the core does not qualify every format.

Choose chunk boundaries around meaning. Splitting a policy paragraph from its exception can produce a plausible but incorrect answer. Markdown-aware chunking retains heading context; a text chunker is useful for simpler material. Keep overlap bounded and store source offsets or section identifiers. Test headings, tables, empty files, unusually long paragraphs, unsupported media and conversion failures. Reject or quarantine failed material rather than indexing an empty success.

Chunk IDs should be stable across retries for the same source revision. A new revision needs an intentional update policy: replace obsolete chunks or preserve versions with an explicit filter. Reusing IDs without revision discipline can leave a mixture of old and new policy. Store enough provenance to reconstruct why a particular answer cited a particular passage.

::: figure art/figures/20-agentic-knowledge.svg | Figure 20.1 — Evidence carries source identity and trusted scope through retrieval

## 20.2. Embeddings and dimensions form a contract

An embedding adapter maps content to vectors. `EmbeddingProtocol` and `BaseEmbedder` define the seam; provider adapters and registries implement it. Use the same embedding model, dimension and normalization policy when indexing and querying. A model migration usually requires re-embedding the corpus or a versioned parallel index. Never mix unrelated vector spaces just because both return lists of floats.

A production fixture should include known relevant and irrelevant documents, multilingual queries and ambiguous wording. Check recall at the retrieval stage before asking whether the final prose sounds good. Caches can reduce repeated embedding work, but cache keys must include the model and its effective configuration. A cached embedding from an older model is not interchangeable with the current one.

For the book, the policy document has vector `[1.0, 0.0]` and the query uses the same fixture vector. This makes cosine ranking transparent. It establishes that a correctly scoped record can be retrieved. It makes no claim about Spanish/English semantic matching, invoice understanding or real provider throughput.

## 20.3. Scope every data operation

The basic vector store API accepts a namespace. The scoped wrapper requires `tenant_id` and `workspace_id` as keyword-only arguments on reads, writes and deletes. It derives a canonical namespace and stamps scope metadata without changing the caller's original document. Lumen derives those identifiers from an authenticated application context; an assistant must not choose its own tenant from prompt text.

::: listing samples/agentic-book/src/lumen_agentic/knowledge.py | Listing 20.1 — A scoped fictional policy with an explicit revision
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
:::

The test retrieves this record for `lumen/help`, receives an empty result for another tenant, and verifies that omitting required scope raises `TypeError`. That negative case matters more than a demonstration that returns one expected document. A production adapter also needs concurrency, deletion, dimension and filter tests against its actual backend.

The native vector-store surface includes in-memory, SQLite vector, PostgreSQL/pgvector, Chroma, Qdrant and Pinecone adapters. Choose by deployment and contract needs, then install the corresponding `agentic-*` extra. Backend filters, index creation and consistency differ. A passing in-memory test cannot prove persistent-store behavior or cross-process isolation. Initialize and close owned clients at application lifecycle boundaries, as Chapter 21 describes.

## 20.4. Retrieve, constrain and validate

Run the query embedding, retrieve a bounded top-k result set, apply authorization and revision filters, then assemble evidence with IDs. If nothing relevant remains, answer that evidence is insufficient or escalate. Do not substitute an ungrounded answer merely because the model can produce one. Similarity is a ranking score, not a probability that a policy applies.

Keep retrieved text visibly separate from instructions. A document may contain malicious or accidental instructions; treat them as untrusted evidence. Tool permissions and domain authorization remain outside that text. Include the policy ID and revision in the requested output, then check that each citation actually appears in the authorized retrieval set. Schema validation only confirms that an ID is a string.

Output validation can use deterministic validators for required evidence, allowed states and invariant checks. Quality-of-service validators and review/refinement loops can improve generated content, but they add calls and failure modes. Set iteration limits and budget them. A model judging another model is still fallible; use human review for consequential exceptions. Record why validation rejected an answer so the operator can distinguish missing evidence from a transient provider error.

## 20.5. Prompts and skills are versioned behavior

Prompt templates separate stable instructions from variables. Supply only the evidence and user context required for the task; version the template alongside its output schema and evaluation dataset. A prompt change can alter tool choice, refusals, citation behavior and cost even when the Python signature stays constant. Treat that as an application change and rerun the cases that protect those properties.

The skills subsystem packages reusable instructions and associated resources. Explicitly select trusted skills for Lumen's assistance role, inspect their resource requirements and avoid loading arbitrary user-supplied instructions as privileged behavior. A skill can teach a process; it cannot confer a permission the application does not grant. The native prompt and skills guides contain the complete loaders, composition and registry APIs.

Conversation memory from Chapter 19 is useful context, but it is not authoritative policy. Keep the current policy revision in retrieved evidence and avoid allowing an old conversation summary to override it. Compression and summarization help fit context limits; preserve exact citations and unresolved human decisions outside lossy summaries. Test a conversation that spans a policy revision.

## 20.6. Choose a reasoning pattern for a measurable need

`pyfly.agentic.reasoning` provides reusable patterns for decomposition, planning, reflection, search and verification. A pattern can break a complex assistance request into manageable steps, but more reasoning steps do not automatically produce a more correct answer. Begin with retrieval plus one typed answer; add a pattern only when a concrete evaluation case shows why the extra work is necessary.

For a multi-policy question, decompose into bounded subquestions, retrieve authorized evidence for each, and synthesize only after validation. Set maximum steps and calls. Provider-supported structured-output modes differ; the runtime's `reasoning_output_mode` and explicit per-pattern options control the strategy. Check the selected provider's supported output path in a separate integration test.

Do not store private reasoning traces as user-facing explanations. Explanations should report the evidence, applied policy and outcome the user can inspect. Explainability tooling can record execution structure and reports without making internal model reasoning a source of truth. Keep prompt/content telemetry opt-in and ensure retention matches Chapter 14's identity and privacy boundaries.

## 20.7. Measure evidence separately from prose

Create a small fictional evaluation set: a normal policy question, no matching policy, an obsolete revision, another tenant's document, an ambiguous question and a document containing hostile instructions. Evaluate retrieval recall, citation validity, schema validity, correct escalation and tool permissions separately. One aggregate score can conceal a dangerous regression in a small category.

The companion's scope test is an executable starting point. Extend it with two workspace IDs within one tenant and a delete followed by a fresh search. Then replace fixture vectors with a real embedding adapter in an explicitly credentialed test lane. Record model/version, dimensions, dataset revision and backend configuration. A production decision needs that evidence plus representative human assessment.

Chapter 21 now connects these bounded components into workflows and operational controls. Keep the distinction clear: evidence retrieval establishes what context the model saw, validation establishes selected output properties, and domain services decide what business operations may occur.

## 20.8. Commit working facts with business data

Install `pyfly[agentic-relational]` for native relational memory. The companion
selects that extra and creates a temporary SQLite file per test. Its
`native_memory(path)` context starts a real `ApplicationContext`; PyFly Data owns
the engine, pool and transaction manager, and Agentic borrows the primary
`DataSource`. A minimal local development configuration is:

```yaml
pyfly:
  agentic:
    enabled: true
    memory-backend: relational
  data:
    relational:
      url: sqlite+aiosqlite:///lumen.db
      ddl-auto: create
```

Use `memory-datasource` to select a registered named datasource. `ddl-auto: create`
is an intentional local bootstrap choice; production `none` or `validate` requires
ordinary schema migrations. Store URLs and pool settings belong in
`pyfly.data.relational`, not a second Agentic connection configuration.

::: listing samples/agentic-book/src/lumen_agentic/native_memory.py | Listing 20.2 — One native unit commits or rolls back the row and fact
async def record_review(
    memory: MemoryManager,
    datasource: DataSource,
    request_id: str,
    *,
    abort: bool = False,
) -> None:
    async with TransactionTemplate(datasource).transaction() as unit:
        assert unit is not None
        await unit.resource.execute(
            text("INSERT INTO review_requests (id) VALUES (:id)"),
            {"id": request_id},
        )
        await memory.async_set_fact(f"review:{request_id}", {"status": "pending"})
        if abort:
            raise ValueError("cancel fictional review")
:::

`TransactionTemplate` joins the same datasource unit used by the memory adapter.
The tests verify a committed row and typed dictionary fact, replacement of an
existing fact, and rollback of both writes when `abort=True`. This is real SQLite
SQL with no model request. Run `uv run pytest -q` from the companion to reproduce
it, or `uv run python -m lumen_agentic.native_memory` to print the committed row,
its fact and the absent rolled-back fact from a temporary local database. Its schema is fictional; it neither authorizes nor moves wallet funds.

Native `RelationalMemoryStore`, `PostgreSQLStore` and `MongoDBStore` borrow Data
resources. Their close operation does not dispose the application's engine or
Mongo client. PostgreSQL retains `firefly_memory.memory_entries`; MongoDB retains
`firefly_memory.entries` unless explicitly configured otherwise. Existing global
entry-ID constraints require an explicit migration to compound namespace/ID
identity. Startup refuses that legacy schema without deleting indexes, rewriting
rows or moving data. Back up and migrate deliberately; changing the configuration
does not perform a destructive migration for you.

## 20.9. Separate vector storage and content encryption

`PgVectorVectorStore` borrows a native relational datasource or transaction
manager and uses its unit of work. Initialize it on the application loop before
business transactions. `create_schema=None` follows the native datasource's
`ddl-auto` policy; explicit bootstrap and production validation remain separate
choices. The old URL and pool arguments are rejected with Data migration guidance.
Install `pyfly[agentic-vectorstores-pgvector]` for that adapter and its native
PostgreSQL dependencies. Vector document IDs retain their existing global
identity; this differs from memory's compound namespace/entry identity.

Encryption is opt-in with `encryption-enabled` and a secret `encryption-key`, or
an injected `EncryptionProvider`; install `pyfly[agentic-security]` for AES support.
The owned-memory wrapper encrypts only `MemoryEntry.content`, preserving JSON
value types. Keys, namespaces, metadata and timestamps remain plaintext, as do
process-local conversations. Do not put secrets into those visible fields.
A missing key, invalid ciphertext or unsupported JSON value fails instead of
silently storing or returning plaintext. User-supplied memory managers remain
borrowed and need their own explicit encryption policy. Qualify key rotation,
recovery and access controls separately from the SQLite transaction test.
