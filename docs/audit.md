# Native audit journal

`pyfly.audit` provides frozen, bounded reference facts and an append-only SQL journal on native PyFly Data. It supports file-backed SQLite and PostgreSQL. It records platform and agent events in the same business transaction, including a durable notification in the native transactional outbox.

The SQL adapter enforces append-only application/database behavior and makes changes detectable against independently trusted heads. It does **not** provide storage-owner immutability, signing-key custody, WORM retention, complete automatic event coverage, enterprise tenancy, or regulatory acceptance. SQLite engine WAL is a database recovery mechanism and is distinct from this business journal.

## Enable and inject

Install `pyfly[data-relational]` (and `pyfly[postgresql]` for PostgreSQL). Enable the native bean:

```yaml
pyfly:
  audit:
    enabled: true
    journal-id: platform
    # datasource: primary  # must be the business datasource
  data:
    relational:
      # url supplied through the application's configuration/secret mechanism
      ddl-auto: none
```

Inject `AuditJournal` from `pyfly.audit`. `AuditAutoConfiguration` resolves the context's DataSourceRegistry and provides a lifecycle-managed `SqlAuditJournal`. It borrows available DI tracer/meter providers; it does not create global telemetry providers or dispose borrowed providers. The journal's `start` must finish before business writes; `stop` leaves data and the caller-owned engine intact. There is no in-memory fallback when storage is unavailable.

For manual assembly use `SqlAuditJournal(datasource, journal_id="platform", create_tables=False)` from `pyfly.audit.sql`, then `await journal.start()` before opening business units. The datasource may be a native transaction manager, registry datasource, registered name or registered async engine. Do not create another engine for the journal beside the business engine.

Audit entry points remain discoverable without SQL, AI or cryptography packages when disabled.
Enabling the default SQL journal or archive without `pyfly[data-relational]` fails startup with an
explicit dependency error; discovery never silently disables configured audit. Recording also requires
its journal and trusted context resolver. A host-provided `AuditJournal` takes precedence over the
default SQL bean, and a host archive delivery override prevents default archive resources from being
created. The Ed25519 adapter's cryptography dependency is separate from custom signer/verifier ports.

For migration-managed deployment, include `framework_metadata` from `pyfly.data.relational.framework_schema` in Alembic metadata. Migrate `pyfly_audit_entries`, `pyfly_audit_heads` and the native `pyfly_outbox_*` tables. SQLAlchemy metadata alone does not install mutation triggers: the migration owner must execute each statement returned by `pyfly.audit.schema.mutation_guard_ddl("postgresql")` (or `"sqlite"`) against that same database. With `ddl-auto: none` startup validates the tables and named enabled guards and refuses missing guards. With `ddl-auto: create` the native journal creates tables/guards at startup; use this for disposable development qualification, not a routine grant to the production application role.

PostgreSQL guard creation uses `CREATE OR REPLACE TRIGGER` and requires PostgreSQL 14 or later. The guards reject UPDATE/DELETE and PostgreSQL TRUNCATE on the entry table. A separate SQLite duplicate-insert guard rejects REPLACE/INSERT OR REPLACE even when SQLite recursive triggers are off. A database owner can replace/disable guards or restore old storage; table/guard validation does not prove an administrator is trustworthy. Keep a separate schema/migration owner. The application role should have SELECT/INSERT on entries, required head SELECT/INSERT/UPDATE and native outbox permissions, with no entry UPDATE/DELETE/TRUNCATE or DDL privileges. These grants and actual intended deployment roles must be qualified in the deployment environment. SQLite file owners have no equivalent server-role separation.

## Append inside the business transaction

```python
from datetime import UTC, datetime
from pyfly.audit import AuditActor, AuditCorrelation, AuditEvent, AuditResource, AuditScope
from pyfly.data.transaction import infrastructure_unit

scope = AuditScope(tenant_id="tenant-example", workspace_id="workspace-example", project_id="project-example")

# In a service, an existing @transactional boundary or SessionProvider.unit() is preferred.
async with infrastructure_unit(business_datasource) as session:
    # Perform the accepted domain write on this native session.
    entry = await journal.append(AuditEvent(
        event_id="operation-123-admitted",  # stable across retries of the same operation
        event_type="run.admitted",
        scope=scope,
        actor=AuditActor(kind="human", subject_id="subject-123"),
        resource=AuditResource(kind="run", resource_id="run-123", revision=1),
        occurred_at=datetime(2026, 10, 31, tzinfo=UTC),
        outcome="admitted",
        correlation=AuditCorrelation(operation_id="operation-123", run_id="run-123"),
    ))
# entry is accepted only after the outer native unit commits.
```

For a custom Store whose `connection()` joins `SessionProvider.unit()` on the same datasource, call `await journal.append(event)` inside that block. Preserve the lock order: business/advisory locks first, then the journal scope's head. When a transaction touches multiple scopes, acquire their journal heads in a consistent sorted order across every producer. Do not call external providers while holding a journal/business transaction.

Append requires an already active native write unit and rejects read-only/autocommit units. Reads and writes reject a bound unit owned by a different transaction manager, even when two application contexts use the same datasource name. It validates facts, serializes callers sharing the same native unit, obtains the scope's database head lock and writes entry, head and outbox notification atomically. Any failed append marks that native unit rollback-only even when business code catches the exception; native savepoint rollback semantics still apply. Storage failures are raised as a static `AuditError` and the native rollback diagnostic is redacted, so a later caught-failure rollback does not expose SQL or payload facts. No manual SQL BEGIN/COMMIT is introduced by the adapter.

The same event ID with identical validated facts returns the original entry and does not enqueue another notification. The same ID with different facts raises `AuditConflict`; keep `occurred_at` stable when retrying the same event. Aware event timestamps normalize to UTC. Ordering is per-scope transaction serialization, not cross-scope wall-clock chronology. A rollback leaves no sequence allocation or accepted entry. External effects require separate intent and observed-result events; unknown outcomes remain unknown until evidence supports a new reconciliation event.

`AuditAdmissionJournal` is a separate optional capability; ordinary `AuditJournal` adapters need not implement it. Its `admit_once(event)` accepts only `intent` facts and uses the same mandatory write unit, scoped ordering, journal and native outbox. It refuses **any** existing scoped event ID with static nonretryable `AuditAdmissionRefused`, including identical facts under a frozen clock. Ordinary append remains idempotent. A trusted producer derives a stable admission ID for the native command and waits for the outer commit before external I/O. An existing or uncertain admission requires explicit reconciliation; it never authorizes another automatic effect. This conservative permission fence does not prove whether a provider executed the effect, offer exactly-once delivery, or provide a refund/replay operation.

An audit recorder/journal configured with an original manager cannot participate in a `RollbackTransaction` or `data_slice(rollback=True)` unit owned by the test's replacement manager. This combination currently fails closed and marks a rejected state write rollback-only; the physical database being the same does not authorize another manager. Qualify audited mutations through explicit native `TransactionTemplate` units in an isolated test database outside that rollback wrapper. Ordinary slice work still distinguishes participating tasks from a fresh context's independent commits through the native context-aware manager selection.

## Reference facts and privacy

Models are frozen, forbid extra fields, revalidate nested models and hide input values in displayed validation errors. Nested collections are bounded tuples. Identifiers contain only letters, digits, underscore, hyphen, dot and colon, up to 128 characters; digest fields require lowercase SHA-256 hex. Costs/counts are bounded nonnegative integers. These are reference contracts, not automatic secret detectors: the producer must supply server-owned opaque identifiers, never put a credential or content into an identifier field.

`AuditEvent` contains scope, actor, resource, event identity/version, UTC occurrence, outcome, typed correlation, up to 16 resource references, up to 16 reason codes, and optional typed usage. There is no arbitrary payload, prompt, completion, node snapshot, free-form error/comment, URL, credential reference value or token field. Policy/model/price/artifact revisions and digests belong in `references`; operation/run/node/worker/receipt/trace IDs belong in `correlation`. `AuditUsage` preserves reserved/measured/unknown/not-reported status and optional microusd/token counts. A missing monetary value is not zero.

The host authorizes scope and actor from verified identity and current memberships. Journal append does not authenticate a caller or install resource tenancy. Grant separate scoped read/export/verify/retention authority at the product boundary. Do not feed generic agent log detail or pipeline input/output snapshots into audit facts. Denied attempts whose business unit rolled back require a separate, explicit post-rollback unit; failure to retain that observation must not be labeled successful capture.

## Read, verify and export

```python
observed_head = await journal.head(scope)  # scoped high-watermark, not verification
page = await journal.read(scope, after_sequence=0, limit=100)
result = await journal.verify(scope, trusted_head=independently_trusted_head)
ndjson = await journal.export(scope, after_sequence=0, through_sequence=100, limit=100)
```

`head(scope)` uses one indexed head-row lookup, without scanning entries, and returns genesis for an empty scope. It is an observed high-watermark, not a verified or independently anchored claim.

Pages contain at most 1,000 frozen `JournalEntry` values. Read checks each entry's canonical bytes, identity and digest. Verification additionally checks the initial physical extent, contiguous sequence, every hash link and the captured head. It captures the head and physical extent in one SQL statement; later concurrent appends do not invalidate the immutable verified prefix. Do not describe a verification result as the latest head forever. If called from an existing write transaction, reads include that transaction's provisional entries; checkpoint signing/export should be done only after its commit.

`verify` without a trusted head reports `anchored=False`. A trusted older head verifies that prefix but the newer local suffix remains unanchored. `anchored=True` means the supplied trusted head equals the verified local boundary, not that this method independently authenticated a signature or a WORM sink. An independently anchored latest head detects stale backup/suffix truncation below that boundary. A valid local prefix alone cannot detect deletion of a valid suffix, replacement of local database and local checkpoint together, or events the producer never emitted.

`export` materializes and checks the exact bounded immutable range, verifies the chain against that range's terminal hash, then returns deterministic UTF-8/ASCII NDJSON from the same frozen values. It performs no fresh storage read after verification. Choose `through_sequence - after_sequence <= limit <= 1000`; out-of-range or corrupt history fails. Empty ranges produce empty bytes. There is no sidecar file written beside a business transaction. The envelope's `pyfly-json-v1` canonicalization is sorted compact Python JSON with ASCII escaping and integer-only numbers; it is explicitly **not** RFC 8785 JCS. Domain-separated SHA-256 binds journal/scope identity, sequence, previous hash and exact versioned facts. Old canonical bytes are not rewritten by schema upcasting.

The native outbox group/destination `pyfly.audit.export` receives only journal ID, typed scope references, chain ID, sequence and entry hash. Install an explicit native relay/consumer when an archive sink is ready. Notifications may arrive out of order or more than once; build contiguous segments from the authoritative journal, deduplicate by immutable entry identity, verify boundary/count/bytes/hash and acknowledge only durable confirmed sink receipts. Normal native outbox pruning does not delete journal entries. This initial journal has no retention-delete API or automatic relay.

## Checkpoint custody and observability

`AuditCheckpoint` binds journal, scope, verified `AuditHead`, issuance time and optional prior checkpoint digest. Its `to_bytes()` defines domain-separated signing bytes. `CheckpointSigner` and `CheckpointVerifier` are async ports exchanging `CheckpointSignature(key_ref, algorithm, signature)`; production custody belongs in an independent KMS/HSM adapter. The core does not persist/sign/anchor checkpoints, trust arbitrary uploaded keys, provision WORM storage or implement regulatory retention. Preserve trusted public-key history and independent latest anchors when integrating those capabilities.

When providers are supplied, the journal emits best-effort `pyfly.audit.operations` counters and `pyfly.audit.append` / `pyfly.audit.verify` spans. Attributes contain only low-cardinality `audit.operation` and `audit.outcome`; no scope IDs, actors, event IDs, digests, content or exception recordings. Append success is labeled `provisional` because the outer unit has not committed. Observability failures do not change mandatory audit atomicity.
