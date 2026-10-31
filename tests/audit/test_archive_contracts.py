# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Deterministic archive facts and unlocked local durable adapters."""

import hashlib
import importlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pyfly.audit import AuditActor, AuditEvent, AuditIntegrityError, AuditResource, AuditScope, JournalEntry
from pyfly.audit.models import chain_id, entry_digest, genesis_hash

SCOPE = AuditScope(tenant_id="local", workspace_id="fixture", project_id="project")


def feature(module, name):
    try:
        value = getattr(importlib.import_module("pyfly.audit." + module), name)
    except (ModuleNotFoundError, AttributeError):
        pytest.fail(f"Native archive contract {module}.{name} is required")
    return value


def entries(count=2):
    chain = chain_id("fixture", SCOPE)
    previous = genesis_hash(chain)
    result = []
    for sequence in range(1, count + 1):
        event = AuditEvent(
            event_id=f"event-{sequence}",
            event_type="run.succeeded",
            scope=SCOPE,
            actor=AuditActor(kind="human", subject_id="actor"),
            resource=AuditResource(kind="run", resource_id="run"),
            occurred_at=datetime(2026, 10, 31, tzinfo=UTC),
            outcome="succeeded",
        )
        entry = JournalEntry(
            journal_id="fixture",
            chain_id=chain,
            sequence=sequence,
            previous_hash=previous,
            entry_hash="0" * 64,
            recorded_at=datetime(2026, 10, 31, tzinfo=UTC),
            event=event,
        )
        entry = entry.model_copy(update={"entry_hash": entry_digest(entry)})
        result.append(entry)
        previous = entry.entry_hash
    return tuple(result)


def test_segment_bytes_and_manifest_are_deterministic_complete_and_bound():
    cls = feature("archive_models", "ArchiveSegment")
    records = entries()
    first, second = cls.from_entries(records), cls.from_entries(records)
    assert first.to_bytes() == second.to_bytes()
    assert first.content_id == hashlib.sha256(first.to_bytes()).hexdigest()
    assert first.manifest.first_sequence == 1 and first.manifest.last_sequence == 2
    assert first.manifest.entry_count == 2 and first.manifest.previous_hash == records[0].previous_hash
    assert first.manifest.terminal_hash == records[-1].entry_hash
    assert first.manifest.payload_bytes == len(first.payload)
    assert first.manifest.payload_digest == hashlib.sha256(first.payload).hexdigest()
    assert cls.from_bytes(first.to_bytes()).entries() == records
    assert "payload=" not in repr(first)
    with pytest.raises(ValidationError):
        first.manifest.entry_count = 3
    with pytest.raises(ValidationError):
        type(first.manifest).model_validate({**first.manifest.model_dump(), "entry_count": True})
    with pytest.raises(ValueError):
        cls.from_entries(records, max_bytes=10)


@pytest.mark.parametrize("mutation", ["truncated", "payload", "gap", "scope", "constructed"])
def test_corrupt_missing_or_forged_segment_facts_are_refused(mutation):
    cls = feature("archive_models", "ArchiveSegment")
    records = entries(3)
    if mutation == "truncated":
        with pytest.raises(ValueError):
            cls.from_bytes(cls.from_entries(records).to_bytes()[:-1])
    elif mutation == "payload":
        with pytest.raises(ValueError):
            cls.from_bytes(cls.from_entries(records).to_bytes().replace(b"run.succeeded", b"run.cancelled"))
    elif mutation == "gap":
        with pytest.raises(ValueError):
            cls.from_entries((records[0], records[-1]))
    elif mutation == "scope":
        forged = records[-1].model_copy(
            update={
                "event": records[-1].event.model_copy(
                    update={"scope": SCOPE.model_copy(update={"project_id": "other"})}
                )
            }
        )
        with pytest.raises(ValueError):
            cls.from_entries((records[0], forged))
    else:
        forged = records[0].model_copy(
            update={"event": records[0].event.model_copy(update={"reason_codes": ("PRIVATE CONTENT",)})}
        )
        with pytest.raises(ValidationError) as failed:
            cls.from_entries((forged,))
        assert "PRIVATE CONTENT" not in str(failed.value)


async def test_local_sink_is_durable_idempotent_and_honestly_unlocked(tmp_path):
    cls = feature("local_archive", "LocalArchiveAdapter")
    root = tmp_path / "archive"
    sink = cls(root, sink_id="fixture")
    assert not root.exists()
    await sink.start()
    caps = sink.capabilities()
    assert caps.conditional_create and caps.durable_ack
    assert not caps.retention_lock and not caps.independent_custody and not caps.versioned
    content = b"fixture public archive bytes"
    digest = hashlib.sha256(content).hexdigest()
    receipt = await sink.put(digest, content)
    assert receipt.retention_status == "unlocked" and receipt.retained_until is None
    assert receipt.content_id == digest and receipt.byte_count == len(content)
    assert await sink.put(digest, content) == receipt
    assert await sink.get(receipt) == content
    await sink.stop()
    reopened = cls(root, sink_id="fixture")
    await reopened.start()
    assert await reopened.get(receipt) == content
    assert await reopened.put(digest, content) == receipt
    with pytest.raises(AuditIntegrityError):
        await reopened.put(digest, b"different")
    assert await reopened.get(receipt) == content
    await reopened.stop()


async def test_local_sink_rejects_corruption_symlinks_and_oversize(tmp_path):
    cls = feature("local_archive", "LocalArchiveAdapter")
    sink = cls(tmp_path / "archive", sink_id="fixture", max_bytes=128)
    await sink.start()
    content = b"original"
    digest = hashlib.sha256(content).hexdigest()
    receipt = await sink.put(digest, content)
    target = tmp_path / "archive/objects" / digest
    target.write_bytes(b"corrupt")
    with pytest.raises(AuditIntegrityError):
        await sink.get(receipt)
    with pytest.raises(AuditIntegrityError):
        await sink.put(digest, content)
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    target.symlink_to(outside)
    with pytest.raises(AuditIntegrityError):
        await sink.get(receipt)
    with pytest.raises(AuditIntegrityError):
        await sink.put(digest, content)
    assert outside.read_bytes() == content
    with pytest.raises(ValueError):
        await sink.put(hashlib.sha256(b"a" * 129).hexdigest(), b"a" * 129)
    await sink.stop()


async def test_ed25519_uses_injected_key_reference_and_binds_exact_body():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    signer_cls = feature("signing", "Ed25519CheckpointSigner")
    verifier_cls = feature("signing", "Ed25519CheckpointVerifier")
    key = Ed25519PrivateKey.generate()
    calls = []

    async def private(reference):
        calls.append(reference)
        return key

    signer = signer_cls("fixture-key", private)
    verifier = verifier_cls(lambda reference: key.public_key() if reference == "fixture-key" else None)
    signature = await signer.sign(b"exact checkpoint bytes")
    assert signature == await signer.sign(b"exact checkpoint bytes")
    assert calls == ["fixture-key", "fixture-key"]
    assert await verifier.verify(b"exact checkpoint bytes", signature)
    assert not await verifier.verify(b"tampered bytes", signature)
    assert not await verifier.verify(b"exact checkpoint bytes", signature.model_copy(update={"key_ref": "unknown"}))
    assert key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex() not in repr(signer)
    assert "signature=" not in repr(signature)


def test_manifest_schema_version_is_strict_and_unknown_fields_are_closed():
    cls = feature("archive_models", "ArchiveSegment")
    manifest = cls.from_entries(entries(1)).manifest
    for invalid in (True, False, "1", 2):
        with pytest.raises(ValidationError):
            type(manifest).model_validate({**manifest.model_dump(), "schema_version": invalid})
    with pytest.raises(ValidationError) as failed:
        type(manifest).model_validate({**manifest.model_dump(), "private_detail": "PRIVATE-CANARY"})
    assert "PRIVATE-CANARY" not in str(failed.value)


async def test_local_reference_projection_is_write_once_and_restartable(tmp_path):
    from pyfly.audit import AuditConflict

    cls = feature("local_archive", "LocalArchiveAdapter")
    receipt_cls = feature("archive_models", "ArchiveDeliveryReceipt")
    sink = cls(tmp_path / "archive", sink_id="fixture")
    await sink.start()
    first = await sink.put(hashlib.sha256(b"first").hexdigest(), b"first")
    second = await sink.put(hashlib.sha256(b"checkpoint").hexdigest(), b"checkpoint")
    third = await sink.put(hashlib.sha256(b"x").hexdigest(), b"x")
    key = "d" * 64
    receipt = receipt_cls(delivery_id=key, segment=first, checkpoint=second)
    assert await sink.lookup(key) is None
    await sink.record(key, receipt)
    await sink.record(key, receipt)
    with pytest.raises(AuditConflict):
        await sink.record(key, receipt.model_copy(update={"checkpoint": third}))
    assert await sink.lookup(key) == receipt
    await sink.stop()
    reopened = cls(tmp_path / "archive", sink_id="fixture")
    await reopened.start()
    assert await reopened.lookup(key) == receipt
    await reopened.stop()


async def test_local_partial_write_is_unaccepted_and_restart_retries_without_overwrite(tmp_path, monkeypatch):
    import os

    from pyfly.audit import AuditError

    cls = feature("local_archive", "LocalArchiveAdapter")
    root = tmp_path / "archive"
    sink = cls(root, sink_id="fixture")
    await sink.start()
    original = os.write
    writes = []

    def partial(descriptor, content):
        if writes:
            raise OSError("PRIVATE-filesystem-fault")
        writes.append(True)
        return original(descriptor, content[:3])

    content = b"complete fixture content"
    digest = hashlib.sha256(content).hexdigest()
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", partial)
        with pytest.raises(AuditError) as failed:
            await sink.put(digest, content)
        assert "PRIVATE" not in str(failed.value)
    assert not (root / "objects" / digest).exists()
    assert list((root / "objects").glob(".pending-*"))
    await sink.stop()
    reopened = cls(root, sink_id="fixture")
    await reopened.start()
    receipt = await reopened.put(digest, content)
    inode = (root / "objects" / digest).stat().st_ino
    assert await reopened.put(digest, content) == receipt
    assert (root / "objects" / digest).stat().st_ino == inode
    assert await reopened.get(receipt) == content
    await reopened.stop()


async def test_directory_sync_failure_does_not_ack_and_retry_heals_durability(tmp_path, monkeypatch):
    import os
    import stat

    from pyfly.audit import AuditError

    cls = feature("local_archive", "LocalArchiveAdapter")
    root = tmp_path / "archive"
    sink = cls(root, sink_id="fixture")
    await sink.start()
    content = b"durable fixture content"
    digest = hashlib.sha256(content).hexdigest()
    original = os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("PRIVATE-directory-sync")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory)
        with pytest.raises(AuditError) as failed:
            await sink.put(digest, content)
        assert "PRIVATE" not in str(failed.value)
    inode = (root / "objects" / digest).stat().st_ino
    receipt = await sink.put(digest, content)
    assert (root / "objects" / digest).stat().st_ino == inode
    assert await sink.get(receipt) == content
    await sink.stop()


async def test_two_adapter_instances_conditional_create_without_replacing_accepted_content(tmp_path):
    import asyncio

    cls = feature("local_archive", "LocalArchiveAdapter")
    left, right = cls(tmp_path / "archive", sink_id="fixture"), cls(tmp_path / "archive", sink_id="fixture")
    await left.start()
    await right.start()
    content = b"one accepted content identity"
    digest = hashlib.sha256(content).hexdigest()
    results = await asyncio.gather(*(adapter.put(digest, content) for adapter in [left, right] * 5))
    assert all(receipt == results[0] for receipt in results)
    assert await right.get(results[0]) == content
    assert [path.name for path in (tmp_path / "archive/objects").iterdir()] == [digest]
    await left.stop()
    await right.stop()


def test_nonregular_fifo_read_is_refused_without_blocking(tmp_path):
    import os
    import subprocess
    import sys

    if not hasattr(os, "mkfifo"):
        pytest.skip("Local POSIX filesystem adapter")
    cls = feature("local_archive", "LocalArchiveAdapter")
    assert cls is not None
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    (root / "objects").mkdir(mode=0o700)
    (root / "refs").mkdir(mode=0o700)
    os.mkfifo(root / "objects" / ("a" * 64))
    script = """
import asyncio,sys
from pyfly.audit import AuditIntegrityError
from pyfly.audit.archive_models import ArchiveReceipt
from pyfly.audit.local_archive import LocalArchiveAdapter
async def main():
    sink=LocalArchiveAdapter(sys.argv[1],sink_id="fixture")
    await sink.start()
    try:
        receipt=ArchiveReceipt(sink_id="fixture",content_id="a"*64,byte_count=0,
                               object_ref="a"*64,retention_status="unlocked")
        await sink.get(receipt)
    except AuditIntegrityError:
        print("refused")
    else:
        raise AssertionError("Nonregular archive target was accepted")
    await sink.stop()
asyncio.run(main())
"""
    completed = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=3)
    assert completed.returncode == 0 and completed.stdout.strip() == "refused", completed.stderr


async def test_new_archive_root_requires_parent_directory_durability(tmp_path, monkeypatch):
    import os

    from pyfly.audit import AuditError

    cls = feature("local_archive", "LocalArchiveAdapter")
    original = os.fsync
    parent_inode = tmp_path.stat().st_ino
    calls = []

    def sync(descriptor):
        inode = os.fstat(descriptor).st_ino
        calls.append(inode)
        if inode == parent_inode:
            raise OSError("PRIVATE-parent-directory-sync")
        original(descriptor)

    sink = cls(tmp_path / "archive", sink_id="fixture")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        with pytest.raises(AuditError) as failed:
            await sink.start()
        assert "PRIVATE" not in str(failed.value)
    assert parent_inode in calls
    await sink.start()
    receipt = await sink.put(hashlib.sha256(b"retry").hexdigest(), b"retry")
    assert await sink.get(receipt) == b"retry"
    await sink.stop()


async def test_successful_publish_cleanup_failure_is_safe_and_not_acknowledged(tmp_path, monkeypatch):
    import os

    from pyfly.audit import AuditError

    cls = feature("local_archive", "LocalArchiveAdapter")
    sink = cls(tmp_path / "archive", sink_id="fixture")
    await sink.start()
    content = b"fixture content"
    digest = hashlib.sha256(content).hexdigest()

    def fail_unlink(*args, **kwargs):
        raise OSError("PRIVATE-cleanup-failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_unlink)
        with pytest.raises(AuditError) as failed:
            await sink.put(digest, content)
        assert "PRIVATE" not in str(failed.value)
    receipt = await sink.put(digest, content)
    assert await sink.get(receipt) == content
    await sink.stop()


async def test_rejected_archive_identifiers_hide_input_values(tmp_path):
    local = feature("local_archive", "LocalArchiveAdapter")
    signer = feature("signing", "Ed25519CheckpointSigner")
    canary = "PRIVATE identifier material"
    for construct in (lambda: local(tmp_path / "archive", sink_id=canary), lambda: signer(canary, lambda key: None)):
        with pytest.raises(ValueError) as failed:
            construct()
        assert canary not in str(failed.value)
    sink = local(tmp_path / "archive", sink_id="fixture")
    for operation in (sink.put(canary, b"fixture"), sink.lookup(canary), sink.record(canary, None)):
        with pytest.raises(ValueError) as failed:
            await operation
        assert canary not in str(failed.value)


async def test_signing_requires_external_trusted_key_history_and_hides_resolver_errors():
    import traceback

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from pyfly.audit import AuditError

    signer_cls = feature("signing", "Ed25519CheckpointSigner")
    verifier_cls = feature("signing", "Ed25519CheckpointVerifier")
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    known = {"old": old.public_key(), "new": new.public_key()}
    verifier = verifier_cls(known.get)
    first = await signer_cls("old", lambda reference: old).sign(b"stable body")
    second = await signer_cls("new", lambda reference: new).sign(b"stable body")
    assert await verifier.verify(b"stable body", first)
    assert await verifier.verify(b"stable body", second)
    assert not await verifier_cls(lambda reference: new.public_key()).verify(b"stable body", first)
    known.pop("old")
    assert not await verifier.verify(b"stable body", first)
    for forged in (
        first.model_copy(update={"algorithm": "other"}),
        first.model_copy(update={"signature": "not-base64"}),
    ):
        assert not await verifier.verify(b"stable body", forged)

    def private_failure(reference):
        raise RuntimeError("PRIVATE resolver credential")

    with pytest.raises(AuditError) as failed:
        await signer_cls("approved-key", private_failure).sign(b"stable body")
    assert "PRIVATE" not in "".join(traceback.format_exception(failed.value))
    assert set(first.model_dump()) == {"key_ref", "algorithm", "signature"}


async def test_cancelled_local_write_finishes_before_descriptor_shutdown_and_retries(tmp_path, monkeypatch):
    import asyncio
    import os
    import threading

    cls = feature("local_archive", "LocalArchiveAdapter")
    root = tmp_path / "archive"
    sink = cls(root, sink_id="fixture")
    await sink.start()
    started, release = threading.Event(), threading.Event()
    original = os.write

    def blocked_write(descriptor, content):
        started.set()
        if not release.wait(3):
            raise TimeoutError("Fixture write was not released")
        return original(descriptor, content)

    content = b"cancelled caller, durable immutable bytes"
    digest = hashlib.sha256(content).hexdigest()
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", blocked_write)
        pending = asyncio.create_task(sink.put(digest, content))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
            await sink.stop()
    reopened = cls(root, sink_id="fixture")
    await reopened.start()
    inode = (root / "objects" / digest).stat().st_ino
    receipt = await reopened.put(digest, content)
    assert (root / "objects" / digest).stat().st_ino == inode
    assert await reopened.get(receipt) == content
    await reopened.stop()
