# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native committed-outbox to signed archive, retry and verification contracts."""

import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pyfly.audit import AuditHead, AuditIntegrityError
from pyfly.audit.models import chain_id, genesis_hash
from pyfly.audit.sql import EXPORT_GROUP, SqlAuditJournal
from pyfly.data.transaction import infrastructure_unit
from pyfly.messaging.listener_container import FixedBackOff, RetryPolicy
from tests.audit.test_archive_contracts import SCOPE, feature
from tests.integration.test_audit_journal import event


async def setup_archive(tmp_path, *, sink=None, receipt_store=None, signer=None, verifier=None):
    cls = feature("archive", "AuditArchiveDelivery")
    local = feature("local_archive", "LocalArchiveAdapter")(tmp_path / "archive", sink_id="fixture")
    await local.start()
    journal = SqlAuditJournal(
        create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"), journal_id="fixture"
    )
    await journal.start()
    key = Ed25519PrivateKey.generate()
    signer = signer or feature("signing", "Ed25519CheckpointSigner")("fixture-key", lambda ref: key)
    verifier = verifier or feature("signing", "Ed25519CheckpointVerifier")(
        lambda ref: key.public_key() if ref == "fixture-key" else None
    )
    delivery = cls(
        journal,
        journal.outbox,
        sink or local,
        receipt_store or local,
        signer,
        verifier,
        journal_id="fixture",
        retry=RetryPolicy(backoff=FixedBackOff(0)),
    )
    return journal, local, delivery, signer, verifier


async def append(journal, identifier):
    async with infrastructure_unit(journal.engine):
        return await journal.append(event(identifier).model_copy(update={"scope": SCOPE}))


async def test_native_outbox_exports_only_committed_facts_and_acks_after_receipts(tmp_path):
    journal, local, delivery, _, verifier = await setup_archive(tmp_path)
    error = feature("archive_ports", "ArchiveError")
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            async with infrastructure_unit(journal.engine):
                await journal.append(event("rolled-back").model_copy(update={"scope": SCOPE}))
                with pytest.raises(error):
                    await delivery.run_once()
                assert not list((tmp_path / "archive/objects").iterdir())
                raise RuntimeError("rollback")
        assert await delivery.run_once() == 0
        entry = await append(journal, "accepted")
        pending = await journal.outbox.pending(EXPORT_GROUP)
        assert len(pending) == 1
        assert await delivery.run_once() == 1
        assert not await journal.outbox.pending(EXPORT_GROUP)
        manifest = feature("archive_models", "ArchiveSegment").from_entries((entry,)).manifest
        receipt = await local.lookup(manifest.delivery_id)
        assert receipt is not None
        result = await feature("archive", "verify_archive")(local, receipt, verifier)
        assert result.entries == (entry,) and result.signature_verified and not result.trusted_head_matched
        assert result.checkpoint.audit.issued_at == entry.recorded_at
        assert result.checkpoint.segment_receipt == receipt.segment
        assert await delivery.deliver(pending[0].envelope) == receipt
        assert len(list((tmp_path / "archive/objects").iterdir())) == 2
        assert len(await journal.read(SCOPE)) == 1
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


async def test_bounded_builder_reads_only_declared_range_and_predecessor(tmp_path, monkeypatch):
    journal, local, delivery, _, _ = await setup_archive(tmp_path)
    records = [await append(journal, f"event-{number}") for number in range(5)]
    reads = []
    read = journal.read

    async def bounded(scope, *, after_sequence=0, limit=100):
        reads.append((after_sequence, limit))
        assert (after_sequence, limit) in {(2, 2), (1, 1)}
        return await read(scope, after_sequence=after_sequence, limit=limit)

    async def unbounded(*args, **kwargs):
        pytest.fail("An archive range must not scan the whole source prefix")

    monkeypatch.setattr(journal, "read", bounded)
    monkeypatch.setattr(journal, "verify", unbounded)
    monkeypatch.setattr(journal, "export", unbounded)
    try:
        result = await feature("archive", "build_archive_segment")(journal, SCOPE, first_sequence=3, last_sequence=4)
        assert result.entries() == tuple(records[2:4])
        assert reads == [(2, 2), (1, 1)]
        async with infrastructure_unit(journal.engine):
            with pytest.raises(feature("archive_ports", "ArchiveError")):
                await feature("archive", "build_archive_segment")(journal, SCOPE, first_sequence=3, last_sequence=4)
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


async def test_out_of_order_delivery_reconstructs_only_supplied_contiguous_verified_chain(tmp_path):
    journal, local, delivery, _, verifier = await setup_archive(tmp_path)
    try:
        entries = [await append(journal, f"event-{number}") for number in range(3)]
        pending = await journal.outbox.pending(EXPORT_GROUP)
        receipts = {}
        for index in (2, 0, 1):
            receipts[index] = await delivery.deliver(pending[index].envelope)
        base = AuditHead(
            chain_id=chain_id("fixture", SCOPE), sequence=0, entry_hash=genesis_hash(chain_id("fixture", SCOPE))
        )
        verify = feature("archive", "verify_archive_chain")
        result = await verify(local, [receipts[index] for index in range(3)], verifier, trusted_head=base)
        assert result.entries == tuple(entries) and result.trusted_head_matched
        assert result.head == entries[-1].head
        for bad in ([receipts[0], receipts[2]], [receipts[1], receipts[0]], [receipts[0], receipts[0]]):
            with pytest.raises(AuditIntegrityError):
                await verify(local, bad, verifier, trusted_head=base)
        with pytest.raises(AuditIntegrityError):
            await verify(local, [receipts[0]], verifier, trusted_head=base.model_copy(update={"entry_hash": "a" * 64}))
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


async def test_forged_notice_identity_never_creates_archive_objects(tmp_path):
    journal, local, delivery, _, _ = await setup_archive(tmp_path)
    try:
        await append(journal, "accepted")
        envelope = (await journal.outbox.pending(EXPORT_GROUP))[0].envelope
        error = feature("archive_ports", "ArchiveError")
        for update in (
            {"entry_hash": "a" * 64},
            {"sequence": True},
            {"journal_id": "other"},
            {"scope": SCOPE.model_copy(update={"project_id": "other"}).model_dump()},
            {"private_input": "PRIVATE-notice"},
        ):
            with pytest.raises(error) as failed:
                await delivery.deliver(replace(envelope, payload={**envelope.payload, **update}))
            assert "PRIVATE" not in str(failed.value)
        assert not list((tmp_path / "archive/objects").iterdir())
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


async def test_wrong_key_tampered_checkpoint_or_receipt_mixup_are_refused(tmp_path):
    journal, local, delivery, _, verifier = await setup_archive(tmp_path)
    try:
        await append(journal, "first")
        await append(journal, "second")
        notices = await journal.outbox.pending(EXPORT_GROUP)
        first = await delivery.deliver(notices[0].envelope)
        second = await delivery.deliver(notices[1].envelope)
        verify = feature("archive", "verify_archive")
        wrong = Ed25519PrivateKey.generate()
        untrusted = feature("signing", "Ed25519CheckpointVerifier")(lambda ref: wrong.public_key())
        with pytest.raises(AuditIntegrityError):
            await verify(local, first, untrusted)
        with pytest.raises(AuditIntegrityError):
            await verify(local, first.model_copy(update={"segment": second.segment}), verifier)
        signed = json.loads((await local.get(first.checkpoint)).removeprefix(b"pyfly-audit-signed-archive-v1\n"))
        signed["checkpoint"]["manifest"]["terminal_hash"] = "a" * 64
        modified = (
            b"pyfly-audit-signed-archive-v1\n" + json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
        )
        alternate = await local.put(__import__("hashlib").sha256(modified).hexdigest(), modified)
        with pytest.raises(AuditIntegrityError):
            await verify(local, first.model_copy(update={"checkpoint": alternate}), verifier)
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


async def test_bounded_builder_revalidates_predecessor_digest_from_a_custom_port(tmp_path, monkeypatch):
    journal, local, delivery, _, _ = await setup_archive(tmp_path)
    try:
        await append(journal, "first")
        await append(journal, "second")
        read = journal.read

        async def altered(scope, *, after_sequence=0, limit=100):
            records = await read(scope, after_sequence=after_sequence, limit=limit)
            if after_sequence == 0 and limit == 1:
                first = records[0]
                return (first.model_copy(update={"event": first.event.model_copy(update={"outcome": "failed"})}),)
            return records

        monkeypatch.setattr(journal, "read", altered)
        with pytest.raises(feature("archive_ports", "ArchiveError")):
            await feature("archive", "build_archive_segment")(journal, SCOPE, first_sequence=2, last_sequence=2)
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


@pytest.mark.parametrize(
    "stage",
    [
        "segment-after",
        "checkpoint",
        "signer",
        "verifier",
        "projection-before",
        "projection-after",
        "projection-read",
        "cancelled-signer",
    ],
)
async def test_native_retry_recovers_each_archive_boundary_without_leaking_or_replaying(tmp_path, stage, caplog):
    import asyncio

    from pyfly.audit._infrastructure import is_audit_infrastructure
    from pyfly.data.transaction import is_transaction_active

    journal, local, original, signer, verifier = await setup_archive(tmp_path)
    await original.stop()
    state = {"failed": False, "puts": 0, "bodies": []}

    def fail(point):
        assert is_audit_infrastructure(journal)
        assert not is_transaction_active()
        if stage == point and not state["failed"]:
            state["failed"] = True
            if point == "cancelled-signer":
                raise asyncio.CancelledError("PRIVATE-adapter-cancellation")
            raise RuntimeError("PRIVATE-adapter-credential")

    class FaultSink:
        def capabilities(self):
            return local.capabilities()

        async def put(self, content_id, content):
            state["puts"] += 1
            if state["puts"] == 2:
                fail("checkpoint")
            receipt = await local.put(content_id, content)
            if state["puts"] == 1:
                fail("segment-after")
            return receipt

        async def get(self, receipt):
            return await local.get(receipt)

    class FaultSigner:
        async def sign(self, body):
            state["bodies"].append(body)
            fail("signer")
            fail("cancelled-signer")
            return await signer.sign(body)

    class FaultVerifier:
        async def verify(self, body, signature):
            fail("verifier")
            return await verifier.verify(body, signature)

    class FaultProjection:
        async def lookup(self, key):
            fail("projection-read")
            return await local.lookup(key)

        async def record(self, key, receipt):
            fail("projection-before")
            await local.record(key, receipt)
            fail("projection-after")

    delivery = feature("archive", "AuditArchiveDelivery")(
        journal,
        journal.outbox,
        FaultSink(),
        FaultProjection(),
        FaultSigner(),
        FaultVerifier(),
        journal_id="fixture",
        retry=RetryPolicy(backoff=FixedBackOff(0)),
    )
    try:
        entry = await append(journal, "accepted-once")
        assert await delivery.run_once() == 1
        assert state["failed"]
        assert delivery.relay.counters.failures == 1
        assert delivery.relay.counters.delivered == 0
        pending = await journal.outbox.pending(EXPORT_GROUP)
        assert len(pending) == 1 and pending[0].attempts == 1
        assert "PRIVATE" not in (pending[0].last_error or "")
        assert "PRIVATE" not in caplog.text
        old_inodes = {path.name: path.stat().st_ino for path in (tmp_path / "archive/objects").iterdir()}
        assert await delivery.run_once() == 1
        assert not await journal.outbox.pending(EXPORT_GROUP)
        assert delivery.relay.counters.delivered == 1
        assert len(await journal.read(SCOPE)) == 1
        manifest = feature("archive_models", "ArchiveSegment").from_entries((entry,)).manifest
        receipt = await local.lookup(manifest.delivery_id)
        result = await feature("archive", "verify_archive")(local, receipt, verifier)
        assert result.entries == (entry,)
        assert len(set(state["bodies"])) <= 1
        assert result.checkpoint.audit.issued_at == entry.recorded_at
        for name, inode in old_inodes.items():
            assert (tmp_path / "archive/objects" / name).stat().st_ino == inode
        assert len(list((tmp_path / "archive/objects").iterdir())) == 2
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()


@pytest.mark.parametrize("mode", ["suspended-write", "suspended-read", "auto-read", "multiple-read-scopes"])
async def test_archive_builder_refuses_every_held_native_data_unit(tmp_path, mode):
    from pyfly.data.relational.sqlalchemy import SqlAlchemyTransactionManager
    from pyfly.data.transaction import Propagation, TransactionTemplate
    from pyfly.data.transaction.template import AutoUnit
    from tests.audit.test_archive_contracts import entries

    record = entries(1)[0]

    class Window:
        calls = 0

        async def head(self, scope):
            self.calls += 1
            return record.head

        async def read(self, scope, *, after_sequence=0, limit=100):
            return (record,)

    source = Window()
    first = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outer.db'}")
    second = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inner.db'}")
    manager = SqlAlchemyTransactionManager(sessionmaker=async_sessionmaker(first, expire_on_commit=False), name="outer")
    other = SqlAlchemyTransactionManager(sessionmaker=async_sessionmaker(second, expire_on_commit=False), name="inner")
    build = feature("archive", "build_archive_segment")
    error = feature("archive_ports", "ArchiveError")

    async def rejected():
        with pytest.raises(error):
            await build(source, SCOPE, first_sequence=1, last_sequence=1)
        assert source.calls == 0

    try:
        if mode.startswith("suspended"):
            async with (
                TransactionTemplate(manager).transaction(read_only=mode.endswith("read")),
                TransactionTemplate(manager).transaction(propagation=Propagation.NOT_SUPPORTED),
            ):
                await rejected()
        elif mode == "auto-read":
            async with AutoUnit(manager, read_only=True, autocommit=True):
                await rejected()
        else:
            async with (
                AutoUnit(manager, read_only=True, autocommit=True),
                AutoUnit(other, read_only=True, autocommit=True),
            ):
                await rejected()
    finally:
        await first.dispose()
        await second.dispose()


async def test_generic_randomized_signer_reuses_accepted_receipt_and_keeps_old_key_trust(tmp_path):
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    from pyfly.audit import CheckpointSignature

    journal, local, original, _, _ = await setup_archive(tmp_path)
    await original.stop()
    private = ec.generate_private_key(ec.SECP256R1())
    trusted = {"external-key-version-1": private.public_key()}
    bodies = []
    frozen = False

    class ExternalSigner:
        async def sign(self, body):
            if frozen:
                pytest.fail("An accepted immutable receipt must not be re-signed after rotation")
            bodies.append(body)
            raw = private.sign(body, ec.ECDSA(hashes.SHA256()))
            return CheckpointSignature(
                key_ref="external-key-version-1",
                algorithm="ECDSA-SHA256",
                signature=base64.urlsafe_b64encode(raw).decode(),
            )

    class TrustedVerifier:
        async def verify(self, body, signature):
            key = trusted.get(signature.key_ref)
            if key is None or signature.algorithm != "ECDSA-SHA256":
                return False
            try:
                key.verify(base64.urlsafe_b64decode(signature.signature), body, ec.ECDSA(hashes.SHA256()))
            except InvalidSignature:
                return False
            return True

    class InterruptedProjection:
        failed = False

        async def lookup(self, key):
            return await local.lookup(key)

        async def record(self, key, receipt):
            if not self.failed:
                self.failed = True
                raise RuntimeError("PRIVATE projection fault")
            await local.record(key, receipt)

    verifier = TrustedVerifier()
    delivery = feature("archive", "AuditArchiveDelivery")(
        journal,
        journal.outbox,
        local,
        InterruptedProjection(),
        ExternalSigner(),
        verifier,
        journal_id="fixture",
        retry=RetryPolicy(backoff=FixedBackOff(0)),
    )
    try:
        entry = await append(journal, "accepted")
        notice = (await journal.outbox.pending(EXPORT_GROUP))[0].envelope
        assert await delivery.run_once() == 1
        assert delivery.relay.counters.failures == 1 and delivery.relay.counters.delivered == 0
        assert await delivery.run_once() == 1
        assert delivery.relay.counters.delivered == 1
        assert not await journal.outbox.pending(EXPORT_GROUP)
        assert len(bodies) == 2 and bodies[0] == bodies[1]
        manifest = feature("archive_models", "ArchiveSegment").from_entries((entry,)).manifest
        accepted = await local.lookup(manifest.delivery_id)
        assert len(list((tmp_path / "archive/objects").iterdir())) == 3
        frozen = True
        assert await delivery.deliver(notice) == accepted
        old_public = trusted.pop("external-key-version-1")
        with pytest.raises(feature("archive_ports", "ArchiveError")):
            await delivery.deliver(notice)
        trusted["external-key-version-1"] = old_public
        assert await delivery.deliver(notice) == accepted
        assert len(list((tmp_path / "archive/objects").iterdir())) == 3
    finally:
        await delivery.stop()
        await local.stop()
        await journal.stop()
        await journal.engine.dispose()
