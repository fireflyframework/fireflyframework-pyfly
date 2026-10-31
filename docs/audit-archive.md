# Audit archive adapters

The archive contracts are provider-neutral. `ArchiveSink` conditionally creates content-addressed objects and reads exact `ArchiveReceipt` versions; `ArchiveReceiptStore` records an immutable delivery lookup separately from the accepted journal. Neither port exposes deletion. AWS/Azure or independent retention adapters can implement these ports without changing journal facts.

`ArchiveCapabilities` distinguishes conditional creation, durable acknowledgement, versioning, retention locks and independent custody. An `ArchiveReceipt` binds the sink, content digest/byte count, opaque object/version reference and actual retention status. Receipts must never contain credentials, signed URLs or endpoint query strings.

## Deterministic bounded segments

`ArchiveSegment.from_entries(entries)` validates a nonempty contiguous sequence of frozen `JournalEntry` facts. Its versioned manifest binds journal/scope/chain, exact first/last sequence/count, previous/terminal entry hashes and NDJSON digest/length. The entire framed object has a content-addressed ID. `from_bytes()` rejects incomplete, noncanonical or inconsistent data. Segments are bounded to 1,000 entries and 16 MiB; callers can choose a smaller byte ceiling.

Formatting an entry does not establish that its business transaction committed. Applications must obtain committed verified journal facts before archiving. An archive operation must not authorize, rewrite accepted business facts or replay external effects.

## Local development adapter

```python
from pyfly.audit.local_archive import LocalArchiveAdapter

# Supply a trusted private parent directory; no path or key is chosen implicitly.
archive = LocalArchiveAdapter(private_archive_directory, sink_id="development")
await archive.start()
try:
    receipt = await archive.put(segment.content_id, segment.to_bytes())
    assert await archive.get(receipt) == segment.to_bytes()
    assert receipt.retention_status == "unlocked"
finally:
    await archive.stop()
```

The local POSIX adapter uses private directories, non-followed descriptor-relative paths, bounded regular-file reads, conditional no-overwrite publication and file/directory fsync before acknowledgement. Identical retries verify accepted bytes without replacing the inode. Failed temporary writes remain unaccepted for explicit inspection; restart retries reuse accepted content. A separate immutable `refs` namespace stores delivery receipts.

Local files remain mutable by their filesystem owner. This adapter is **unlocked** and provides no retention lock, independent custody, WORM or regulatory acceptance. Closing it releases only its descriptors and preserves files. There is no automatic deletion or cloud configuration.

## Explicit signing authority

`Ed25519CheckpointSigner(key_ref, resolve_private_key)` and `Ed25519CheckpointVerifier(resolve_public_key)` implement the existing checkpoint ports using the native `cryptography` dependency (`pyfly[security]`). Callbacks may be synchronous or asynchronous and must resolve approved key references; private keys are never generated or persisted by the adapter. Supply trusted public-key history for verification after rotation.

Signing uses bounded exact bytes and records only the key reference, algorithm and signature. Callback failures expose safe errors; representations and metadata omit private key material. Tests generate fixture keys. Signature validity alone does not establish independent archive anchoring, latest-head completeness or retention protection.

## Optional native composition

Enable the archive explicitly, alongside the journal on the business datasource:

```yaml
pyfly:
  audit:
    enabled: true
    journal-id: platform
    archive:
      enabled: true
      adapter: local
      path: ${AUDIT_ARCHIVE_PATH}
      sink-id: development
      max-bytes: 16777216
      poll-interval-seconds: 5.0
```

Provide the trusted private parent directory and required journal/outbox migrations separately. `AuditArchiveAutoConfiguration` creates one local storage instance, aliases its two ports and composes the native relay on the journal's configured datasource. It never creates a signing key. Supply `CheckpointSigner` and `CheckpointVerifier` beans explicitly, for example through application-owned callbacks to approved secret references and trusted public-key history. Missing mandatory ports fail startup before storage lifecycle begins. Disabled configuration creates no archive path or key. Custom sink/projection ports can replace local storage; custom journal/outbox implementations can compose `AuditArchiveDelivery` directly.

```python
from pyfly.audit import CheckpointSigner, CheckpointVerifier
from pyfly.audit.signing import Ed25519CheckpointSigner, Ed25519CheckpointVerifier
from pyfly.container import bean, configuration

@configuration
class ArchiveAuthority:
    @bean
    def archive_signer(self) -> CheckpointSigner:
        return Ed25519CheckpointSigner("approved-audit-key-v1", private_key_resolver)

    @bean
    def archive_verifier(self) -> CheckpointVerifier:
        return Ed25519CheckpointVerifier(trusted_public_key_resolver)
```

The callbacks above are application-supplied authority, not keys embedded in configuration or a receipt. For manually selected auto-configurations, include both `AuditAutoConfiguration` and `AuditArchiveAutoConfiguration`. Native lifecycle starts storage before the consumer and stops the consumer before closing local descriptors. Cancelled start/stop waits for its owned filesystem operation to settle; a cancelled start closes descriptors it opened. Cancelled writes may finish an unacknowledged content object that a retry safely verifies/reuses. No global executor or process lifecycle is replaced.

## Committed delivery and bounded verification

`AuditArchiveDelivery` borrows `AuditJournal`, the matching native `OutboxStore`, `ArchiveSink`, `ArchiveReceiptStore` and signing ports. It composes **the existing** `OutboxRelay` with group `pyfly.audit.export`, `register=False`, `transactional=False` and no automatic pruning. `start()/stop()` manage that relay; `run_once()` uses its normal claims, leases, retries and acknowledgements. The export destination is reserved for the journal's typed, server-owned notices.

Every notice produces one entry segment, one signed-checkpoint content object and one durable delivery-reference record. Out-of-order/duplicate notices need no new cursor or queue. The worker validates exact journal/scope/chain/sequence/hash, then reads the declared source range and at most one predecessor. It does not scan the whole historical prefix per notice. Active/completing native Data units, including suspended units and read-auto scopes on any datasource, forbid delivery/build work. Only committed facts are eligible; a rolled-back journal/outbox unit produces no archive delivery.

`ArchiveCheckpoint` binds the manifest and **exact segment receipt/object version** to the existing scoped `AuditCheckpoint`. `SignedArchiveCheckpoint` stores that body and its signature as a separate content-addressed object. Its own receipt is outside the signature to avoid a self-reference. Logical `issued_at` comes from the committed terminal entry's `recorded_at`, not the retry clock; it is not an independently certified signing timestamp.

Before ack, the worker rereads objects, checks bytes/ranges/digests, verifies through the **injected** trusted key registry and confirms the immutable delivery projection. Sink, signer, verifier and projection failures expose static safe delivery errors; cancellation preserves its kind without retaining third-party messages in retry/DLQ errors. Failure after a partial archive write retries archival only; it never rewrites accepted business facts or replays their effects. No recursive archive-confirmed event is appended to the same journal.

Ed25519 with the same key produces deterministic signatures. A generic signer may return different valid signatures for the same stable body, producing unaccepted candidate objects after interrupted attempts. There is no generic byte-idempotence or exactly-once signer-billing claim. Once a delivery reference is accepted, retries recover and verify that exact receipt without re-signing it. Retain trusted historical public keys across rotation; an unknown/revoked verification key fails closed instead of replacing accepted evidence.

For explicit manual ranges:

```python
from pyfly.audit.archive import build_archive_segment, verify_archive, verify_archive_chain

segment = await build_archive_segment(
    journal, scope, first_sequence=first, last_sequence=last,
    max_bytes=1_048_576,
)
verified = await verify_archive(sink, accepted_receipt, trusted_verifier)
chain = await verify_archive_chain(
    sink, ordered_receipts, trusted_verifier, trusted_head=independent_boundary,
)
```

The builder validates at most 1,000 entries and one predecessor. Single/chain verification validates exact stored frames, receipt versions, signatures and contiguous links. A chain call is bounded to 1,000 segments/entries and 16 MiB of declared object bytes. Supplied ranges may be partial: `starts_at_genesis` and `trusted_head_matched` describe the actual evidence. A trusted boundary must match the immediate predecessor or an entry in the supplied range. Signature validity alone proves neither independent custody, completeness through the latest source head nor WORM. Verification returns frozen facts only; it restores no business tables and replays no effects.

One-notice/one-entry archival has per-object/signing/fsync costs. `max-bytes` and polling are configurable; the manual builder supports bounded larger segments. The automatic worker does not batch several notices into new aggregate segments or add a second delivery queue. Qualify throughput and provider costs before a deployment claim.

## Operational evidence and observability

`delivery.relay` exposes the native `OutboxRelay` for authorized server-side diagnostics:

- `state`, `running`, `poll_interval` and `last_error` describe this process. `last_error` is the last failed **round**, not every handled delivery failure.
- `counters.delivered`, `failures`, `dead_lettered`, `dead_letter_store_failures`, `failed_rounds` and `pruned` are in-memory counters since relay construction; they are not durable global totals or automatically exported OpenTelemetry metrics.
- `await delivery.relay.outbox.pending("pyfly.audit.export", limit=...)` returns a bounded oldest-first page, including claimed rows, with `attempts`, `available_at` and safe handler `last_error`. Page length is not a total backlog gauge.
- `await delivery.relay.outbox.dead_letters("pyfly.audit.export", limit=...)` returns a bounded recent DLQ page. Investigate and authorize recovery; the archive layer provides no automatic business-effect replay.
- `ArchiveReceiptStore.lookup(delivery_id)` survives restart and returns the exact accepted receipt reference. Verify its objects/signature before reporting durable evidence. There is no automatic global/latest archive cursor; missing one ID does not mean no historical archives. The last receipt observed by a process is not necessarily the highest source sequence because notices may arrive out of order.

Core journal counters/spans describe journal operations; they do not imply archive delivery success/latency coverage. This slice adds no automatic archive success/failure metric, latency histogram, backlog gauge or hosted dashboard. If an application exports aggregates, use low-cardinality operation/outcome/backend labels. Never use journal/scope/actor/event IDs, object IDs, digests or signatures as metric labels. Authorize scope-specific operator queries and sanitize native queue/storage diagnostics before exposing them to clients.
