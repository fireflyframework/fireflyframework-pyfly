# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Bounded committed-journal archival through native outbox claims, retries and acknowledgements."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

from pydantic import ConfigDict, TypeAdapter

from pyfly.audit._infrastructure import audit_infrastructure, has_active_data_units
from pyfly.audit.archive_models import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_ENTRIES,
    ArchiveCapabilities,
    ArchiveChainVerification,
    ArchiveCheckpoint,
    ArchiveDeliveryReceipt,
    ArchiveReceipt,
    ArchiveSegment,
    ArchiveVerification,
    AuditArchiveNotice,
    SignedArchiveCheckpoint,
    byte_limit,
    content_digest,
)
from pyfly.audit.archive_ports import ArchiveError, ArchiveReceiptStore, ArchiveSink
from pyfly.audit.models import (
    AuditCheckpoint,
    AuditHead,
    AuditScope,
    CheckpointSignature,
    Identifier,
    JournalEntry,
    entry_digest,
    genesis_hash,
)
from pyfly.audit.ports import AuditConflict, AuditIntegrityError, AuditJournal, CheckpointSigner, CheckpointVerifier
from pyfly.eda.outbox import OutboxRelay
from pyfly.eda.ports.outbox import OutboxStore
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.listener_container import RetryPolicy

EXPORT_GROUP = "pyfly.audit.export"


def _outside_unit() -> None:
    if has_active_data_units():
        raise ArchiveError("Archive delivery requires committed facts outside every active Data unit")


async def build_archive_segment(
    journal: AuditJournal,
    scope: AuditScope,
    *,
    first_sequence: int,
    last_sequence: int,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> ArchiveSegment:
    """Validate a declared committed range and one predecessor, never scan its entire source prefix."""
    try:
        _outside_unit()
        byte_limit(max_bytes)
        if (
            type(first_sequence) is not int
            or type(last_sequence) is not int
            or not 1 <= first_sequence <= last_sequence <= 2**63 - 1
            or last_sequence - first_sequence + 1 > MAX_ARCHIVE_ENTRIES
        ):
            raise ValueError
        scope = AuditScope.model_validate(scope)
        with audit_infrastructure(journal):
            head = AuditHead.model_validate(await journal.head(scope))
            if head.sequence < last_sequence:
                raise ValueError
            records = await journal.read(
                scope, after_sequence=first_sequence - 1, limit=last_sequence - first_sequence + 1
            )
            result = ArchiveSegment.from_entries(records, max_bytes=max_bytes)
            manifest = result.manifest
            if (
                manifest.scope != scope
                or manifest.chain_id != head.chain_id
                or manifest.first_sequence != first_sequence
                or manifest.last_sequence != last_sequence
            ):
                raise ValueError
            if first_sequence > 1:
                previous = await journal.read(scope, after_sequence=first_sequence - 2, limit=1)
                if len(previous) != 1:
                    raise ValueError
                predecessor = JournalEntry.model_validate(previous[0])
                if (
                    predecessor.sequence != first_sequence - 1
                    or predecessor.chain_id != manifest.chain_id
                    or predecessor.journal_id != manifest.journal_id
                    or predecessor.event.scope != scope
                    or predecessor.entry_hash != manifest.previous_hash
                    or entry_digest(predecessor) != predecessor.entry_hash
                ):
                    raise ValueError
            if last_sequence == head.sequence and head.entry_hash != manifest.terminal_hash:
                raise ValueError
            return result
    except asyncio.CancelledError:
        raise asyncio.CancelledError from None
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("Committed archive range failed bounded verification") from None


def _trusted_boundary(records: tuple[JournalEntry, ...], trusted_head: AuditHead | None) -> bool:
    if trusted_head is None:
        return False
    trusted_head = AuditHead.model_validate(trusted_head)
    first = records[0]
    if trusted_head.chain_id != first.chain_id:
        raise ValueError
    if trusted_head.sequence == first.sequence - 1 and trusted_head.entry_hash == first.previous_hash:
        return True
    if any(record.head == trusted_head for record in records):
        return True
    raise ValueError


async def verify_archive(
    sink: ArchiveSink,
    receipt: ArchiveDeliveryReceipt,
    verifier: CheckpointVerifier,
    *,
    trusted_head: AuditHead | None = None,
) -> ArchiveVerification:
    """Verify one archived range; only an explicit caller-supplied boundary conveys independent trust."""
    try:
        receipt = ArchiveDeliveryReceipt.model_validate(receipt)
        checkpoint_bytes = await sink.get(receipt.checkpoint)
        if type(checkpoint_bytes) is not bytes or len(checkpoint_bytes) > 32768:
            raise ValueError
        if (
            content_digest(checkpoint_bytes) != receipt.checkpoint.content_id
            or len(checkpoint_bytes) != receipt.checkpoint.byte_count
        ):
            raise ValueError
        signed = SignedArchiveCheckpoint.from_bytes(checkpoint_bytes)
        if (await verifier.verify(signed.checkpoint.to_bytes(), signed.signature)) is not True:
            raise ValueError
        body = signed.checkpoint
        if body.segment_receipt != receipt.segment:
            raise ValueError
        segment_bytes = await sink.get(receipt.segment)
        if (
            type(segment_bytes) is not bytes
            or len(segment_bytes) > MAX_ARCHIVE_BYTES
            or content_digest(segment_bytes) != receipt.segment.content_id
            or len(segment_bytes) != receipt.segment.byte_count
        ):
            raise ValueError
        segment = ArchiveSegment.from_bytes(segment_bytes)
        records = segment.entries()
        if (
            body.manifest != segment.manifest
            or receipt.delivery_id != segment.manifest.delivery_id
            or body.audit.issued_at != records[-1].recorded_at
        ):
            raise ValueError
        return ArchiveVerification(
            checkpoint=body, entries=records, trusted_head_matched=_trusted_boundary(records, trusted_head)
        )
    except asyncio.CancelledError:
        raise asyncio.CancelledError from None
    except Exception:
        raise AuditIntegrityError("Archive receipt, signature or content verification failed") from None


async def verify_archive_chain(
    sink: ArchiveSink,
    receipts: Sequence[ArchiveDeliveryReceipt],
    verifier: CheckpointVerifier,
    *,
    trusted_head: AuditHead | None = None,
) -> ArchiveChainVerification:
    """Verify a bounded ordered segment chain without restoring business state or replaying effects."""
    try:
        if not isinstance(receipts, (tuple, list)) or not 1 <= len(receipts) <= MAX_ARCHIVE_ENTRIES:
            raise ValueError
        receipts = tuple(ArchiveDeliveryReceipt.model_validate(receipt) for receipt in receipts)
        if sum(receipt.segment.byte_count + receipt.checkpoint.byte_count for receipt in receipts) > MAX_ARCHIVE_BYTES:
            raise ValueError
        records: list[JournalEntry] = []
        for receipt in receipts:
            result = await verify_archive(sink, receipt, verifier)
            if len(records) + len(result.entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError
            if records and (
                result.entries[0].sequence != records[-1].sequence + 1
                or result.entries[0].previous_hash != records[-1].entry_hash
                or result.entries[0].chain_id != records[-1].chain_id
                or result.entries[0].journal_id != records[-1].journal_id
                or result.entries[0].event.scope != records[-1].event.scope
            ):
                raise ValueError
            records.extend(result.entries)
        first = records[0]
        values = tuple(records)
        return ArchiveChainVerification(
            entries=values,
            segment_count=len(receipts),
            starts_at_genesis=first.sequence == 1 and first.previous_hash == genesis_hash(first.chain_id),
            trusted_head_matched=_trusted_boundary(values, trusted_head),
        )
    except asyncio.CancelledError:
        raise asyncio.CancelledError from None
    except Exception:
        raise AuditIntegrityError("Archive segments failed bounded chain verification") from None


class AuditArchiveDelivery:
    """Borrowed ports; only the native relay is stopped. Outbox acknowledgements follow durable readback."""

    phase = CONSUMER_PHASE

    @property
    def relay(self) -> OutboxRelay:
        """Native process counters/state and bounded outbox diagnostics; not an exported metrics service."""
        return self._relay

    def __init__(
        self,
        journal: AuditJournal,
        outbox: OutboxStore,
        sink: ArchiveSink,
        receipt_store: ArchiveReceiptStore,
        signer: CheckpointSigner,
        verifier: CheckpointVerifier,
        *,
        journal_id: str = "default",
        max_bytes: int = MAX_ARCHIVE_BYTES,
        retry: RetryPolicy | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        byte_limit(max_bytes)
        self._journal_id = TypeAdapter(Identifier, config=ConfigDict(hide_input_in_errors=True)).validate_python(
            journal_id
        )
        self._journal, self._outbox, self._sink = journal, outbox, sink
        self._receipts, self._signer, self._verifier = receipt_store, signer, verifier
        self._max_bytes = max_bytes
        self._relay = OutboxRelay(
            outbox,
            group=EXPORT_GROUP,
            destinations=[EXPORT_GROUP],
            register=False,
            transactional=False,
            retry=retry,
            retention=None,
            poll_interval=poll_interval,
        )
        self._relay.subscribe("audit.entry.appended", self.handle)
        caps = self.capabilities()
        if not caps.conditional_create or not caps.durable_ack:
            raise ArchiveError("Archive delivery requires conditional creation and durable acknowledgement")

    def capabilities(self) -> ArchiveCapabilities:
        try:
            return ArchiveCapabilities.model_validate(self._sink.capabilities())
        except Exception:
            raise ArchiveError("Archive capabilities are unavailable") from None

    async def start(self) -> None:
        try:
            _outside_unit()
            with audit_infrastructure(self._journal):
                await self._outbox.start()
                await self._relay.start()
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            raise ArchiveError("Archive delivery startup failed") from None

    async def stop(self) -> None:
        with audit_infrastructure(self._journal):
            await self._relay.stop()

    async def run_once(self) -> int:
        try:
            _outside_unit()
            with audit_infrastructure(self._journal):
                return await self._relay.run_once()
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            raise ArchiveError("Archive delivery round failed") from None

    async def handle(self, envelope: EventEnvelope) -> None:
        await self.deliver(envelope)

    async def _accepted(self, receipt: ArchiveDeliveryReceipt, segment: ArchiveSegment) -> ArchiveDeliveryReceipt:
        receipt = ArchiveDeliveryReceipt.model_validate(receipt)
        verified = await verify_archive(self._sink, receipt, self._verifier)
        if verified.checkpoint.manifest != segment.manifest or receipt.segment.content_id != segment.content_id:
            raise ArchiveError("Archived delivery reference does not match its committed source range")
        return receipt

    async def _put(self, content: bytes) -> ArchiveReceipt:
        receipt = ArchiveReceipt.model_validate(await self._sink.put(content_digest(content), content))
        caps = self.capabilities()
        if (
            receipt.sink_id != caps.sink_id
            or receipt.content_id != content_digest(content)
            or receipt.byte_count != len(content)
            or (receipt.retention_status == "locked" and not caps.retention_lock)
        ):
            raise ArchiveError("Archive sink returned an inconsistent durable receipt")
        if await self._sink.get(receipt) != content:
            raise ArchiveError("Archive sink readback does not match accepted content")
        return receipt

    async def deliver(self, envelope: EventEnvelope) -> ArchiveDeliveryReceipt:
        try:
            _outside_unit()
            with audit_infrastructure(self._journal):
                if envelope.event_type != "audit.entry.appended" or envelope.destination != EXPORT_GROUP:
                    raise ValueError
                notice = AuditArchiveNotice.model_validate(envelope.payload)
                if notice.journal_id != self._journal_id:
                    raise ValueError
                segment = await build_archive_segment(
                    self._journal,
                    notice.scope,
                    first_sequence=notice.sequence,
                    last_sequence=notice.sequence,
                    max_bytes=self._max_bytes,
                )
                if (
                    segment.manifest.journal_id != notice.journal_id
                    or segment.manifest.chain_id != notice.chain_id
                    or segment.manifest.terminal_hash != notice.entry_hash
                ):
                    raise ValueError
                existing = await self._receipts.lookup(segment.manifest.delivery_id)
                if existing is not None:
                    return await self._accepted(existing, segment)
                segment_receipt = await self._put(segment.to_bytes())
                terminal = segment.entries()[-1]
                body = ArchiveCheckpoint(
                    audit=AuditCheckpoint(
                        journal_id=notice.journal_id,
                        scope=notice.scope,
                        head=terminal.head,
                        issued_at=terminal.recorded_at,
                    ),
                    manifest=segment.manifest,
                    segment_receipt=segment_receipt,
                )
                signature = CheckpointSignature.model_validate(await self._signer.sign(body.to_bytes()))
                if (await self._verifier.verify(body.to_bytes(), signature)) is not True:
                    raise ValueError
                checkpoint_receipt = await self._put(
                    SignedArchiveCheckpoint(checkpoint=body, signature=signature).to_bytes()
                )
                receipt = ArchiveDeliveryReceipt(
                    delivery_id=segment.manifest.delivery_id, segment=segment_receipt, checkpoint=checkpoint_receipt
                )
                await self._accepted(receipt, segment)
                with contextlib.suppress(AuditConflict):
                    await self._receipts.record(receipt.delivery_id, receipt)
                accepted = await self._receipts.lookup(receipt.delivery_id)
                if accepted is None:
                    raise ValueError
                return await self._accepted(accepted, segment)
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            raise ArchiveError("Archive delivery failed; no acknowledgement was confirmed") from None
