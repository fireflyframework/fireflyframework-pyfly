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
