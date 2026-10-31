# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Transaction-ordered audit journal on native PyFly Data and transactional outbox.

This enforces application/database append-only semantics, not storage-owner WORM. A receipt returned
inside a unit is provisional until the caller's business transaction commits. Start before business units.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.audit.models import (
    AuditEvent,
    AuditHead,
    AuditScope,
    Identifier,
    JournalEntry,
    VerificationResult,
    canonical_bytes,
    chain_id,
    entry_digest,
    genesis_hash,
    utc_now,
)
from pyfly.audit.ports import AuditConflict, AuditError, AuditIntegrityError
from pyfly.audit.schema import ensure_mutation_guards
from pyfly.data.relational.dialect_customizers import is_file_database
from pyfly.data.relational.framework_schema import audit_entries, audit_heads, ensure_tables
from pyfly.data.relational.upsert import insert_if_absent
from pyfly.data.transaction import current_unit_of_work, infrastructure_unit, resolve_manager
from pyfly.eda.outbox import SqlOutboxStore
from pyfly.eda.types import EventEnvelope

EXPORT_GROUP = "pyfly.audit.export"


class SqlAuditJournal:
    def __init__(
        self,
        datasource: object = None,
        *,
        journal_id: str = "default",
        create_tables: bool = True,
        tracer_provider: Any = None,
        meter_provider: Any = None,
    ) -> None:
        self._target = datasource
        try:
            self.journal_id = TypeAdapter(Identifier).validate_python(journal_id)
        except ValueError:
            raise AuditError("Invalid audit journal identifier") from None
        self._create = create_tables
        self._started = False
        self._tracer = None
        self._counter = None
        try:
            if tracer_provider is not None:
                self._tracer = tracer_provider.get_tracer("pyfly.audit")
            if meter_provider is not None:
                self._counter = meter_provider.get_meter("pyfly.audit").create_counter(
                    "pyfly.audit.operations", unit="1"
                )
        except Exception:
            pass
        self.outbox = SqlOutboxStore(datasource, create_tables=create_tables)

    @property
    def engine(self) -> AsyncEngine:
        manager = resolve_manager(self._target)
        engine = getattr(manager, "engine", None)
        if not isinstance(engine, AsyncEngine):
            raise AuditError("Audit journal requires a native relational datasource")
        return engine

    async def start(self) -> None:
        if self._started:
            return
        manager = resolve_manager(self._target)
        if current_unit_of_work(manager.datasource) is not None:
            raise AuditError("Start the audit journal before opening business transactions")
        dialect = self.engine.dialect.name
        if dialect == "sqlite" and not is_file_database(self.engine.url):
            raise AuditError("Audit journal requires file-backed SQLite storage")
        if dialect not in {"sqlite", "postgresql"}:
            raise AuditError("Audit journal supports SQLite and PostgreSQL only")
        await ensure_tables(self._target, audit_entries, audit_heads, create=self._create)
        await ensure_mutation_guards(self._target, dialect, create=self._create)
        await self.outbox.start()
        self._started = True

    async def stop(self) -> None:
        self._started = False
        await self.outbox.stop()

    def _ready(self) -> None:
        if not self._started:
            raise AuditError("Start the audit journal before use")

    def _identity(self, scope: AuditScope) -> str:
        return chain_id(self.journal_id, AuditScope.model_validate(scope))

    def _observe(self, operation: str, outcome: str) -> None:
        attributes = {"audit.operation": operation, "audit.outcome": outcome}
        try:
            if self._counter is not None:
                self._counter.add(1, attributes)
            if self._tracer is not None:
                with self._tracer.start_as_current_span(
                    "pyfly.audit." + operation,
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    span.set_attributes(attributes)
        except Exception:
            pass

    async def append(self, event: AuditEvent) -> JournalEntry:
        manager = resolve_manager(self._target)
        unit = current_unit_of_work(manager.datasource)
        try:
            if unit is not None and unit.autocommit:
                raise AuditError("Audit append requires an atomic native Data write unit")
            result = await self._append(event)
        except BaseException as error:
            if unit is not None:
                unit.set_rollback_only("Audit append failed")
                unit.redact_rollback_reason("Audit append failed")
            self._observe("append", "failed")
            if isinstance(error, SQLAlchemyError):
                raise AuditError("Audit journal append failed") from None
            raise
        self._observe("append", "provisional")
        return result

    async def _append(self, event: AuditEvent) -> JournalEntry:
        self._ready()
        # Revalidate untrusted model_copy/model_construct instances before touching the transaction.
        event = AuditEvent.model_validate(event)
        manager = resolve_manager(self._target)
        unit = current_unit_of_work(manager.datasource)
        if unit is None:
            raise AuditError("Audit append requires an active native Data write unit")
        if unit.read_only:
            raise AuditError("Audit append requires a write unit")
        chain = self._identity(event.scope)
        async with infrastructure_unit(manager) as session, unit.operation():
            await insert_if_absent(
                session,
                audit_heads,
                {
                    "chain_id": chain,
                    "sequence": 0,
                    "entry_hash": genesis_hash(chain),
                },
                key=["chain_id"],
            )
            head = (
                (await session.execute(select(audit_heads).where(audit_heads.c.chain_id == chain).with_for_update()))
                .mappings()
                .one()
            )
            prior = (
                (
                    await session.execute(
                        select(audit_entries).where(
                            audit_entries.c.chain_id == chain,
                            audit_entries.c.event_id == event.event_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if prior is not None:
                existing = self._decode(prior, event.scope)
                if canonical_bytes(existing.event.model_dump(mode="json")) != canonical_bytes(
                    event.model_dump(mode="json")
                ):
                    raise AuditConflict("Audit event ID already identifies different facts")
                return existing
            sequence = int(head["sequence"]) + 1
            previous_hash = str(head["entry_hash"])
            if sequence > 1:
                previous = (
                    (
                        await session.execute(
                            select(audit_entries).where(
                                audit_entries.c.chain_id == chain,
                                audit_entries.c.sequence == sequence - 1,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous is None or self._decode(previous, event.scope).entry_hash != previous_hash:
                    raise AuditIntegrityError("Audit head does not match its preceding entry")
            elif previous_hash != genesis_hash(chain):
                raise AuditIntegrityError("Audit genesis head is invalid")
            entry = JournalEntry(
                journal_id=self.journal_id,
                chain_id=chain,
                sequence=sequence,
                previous_hash=previous_hash,
                entry_hash="0" * 64,
                recorded_at=utc_now(),
                event=event,
            )
            entry = entry.model_copy(update={"entry_hash": entry_digest(entry)})
            await session.execute(
                insert(audit_entries).values(
                    chain_id=chain,
                    sequence=sequence,
                    event_id=event.event_id,
                    entry_hash=entry.entry_hash,
                    payload=entry.to_bytes().decode("ascii"),
                )
            )
            changed = await session.execute(
                update(audit_heads)
                .where(
                    audit_heads.c.chain_id == chain,
                    audit_heads.c.sequence == sequence - 1,
                    audit_heads.c.entry_hash == previous_hash,
                )
                .values(sequence=sequence, entry_hash=entry.entry_hash)
            )
            if changed.rowcount != 1:
                raise AuditConflict("Audit head changed concurrently; retry the business transaction")
            await self.outbox.append(
                EventEnvelope(
                    event_type="audit.entry.appended",
                    destination=EXPORT_GROUP,
                    payload={
                        "journal_id": self.journal_id,
                        "chain_id": chain,
                        "sequence": sequence,
                        "entry_hash": entry.entry_hash,
                    },
                ),
                groups=[EXPORT_GROUP],
            )
            return entry

    def _decode(self, row: Any, scope: AuditScope) -> JournalEntry:
        try:
            entry = JournalEntry.model_validate_json(row["payload"])
            if (
                entry.journal_id != self.journal_id
                or entry.event.scope != scope
                or entry.chain_id != self._identity(scope)
                or entry.chain_id != row["chain_id"]
                or entry.sequence != row["sequence"]
                or entry.event.event_id != row["event_id"]
                or entry.entry_hash != row["entry_hash"]
                or entry_digest(entry) != entry.entry_hash
                or entry.to_bytes().decode("ascii") != row["payload"]
            ):
                raise ValueError
            return entry
        except Exception:
            raise AuditIntegrityError("Audit entry failed canonical integrity verification") from None

    @staticmethod
    def _bounds(after_sequence: int, limit: int) -> None:
        if type(after_sequence) is not int or not 0 <= after_sequence <= 2**63 - 1:
            raise ValueError("Audit cursor must be a nonnegative bounded integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Audit page limit must be between 1 and 1000")

    async def head(self, scope: AuditScope) -> AuditHead:
        """Observe the scoped high-watermark in one indexed lookup; this is not chain verification."""
        self._ready()
        chain = self._identity(scope)
        async with infrastructure_unit(self._target, read_only=True) as session:
            found = (
                (await session.execute(select(audit_heads).where(audit_heads.c.chain_id == chain))).mappings().first()
            )
            try:
                return (
                    AuditHead(chain_id=chain, sequence=0, entry_hash=genesis_hash(chain))
                    if found is None
                    else AuditHead(**found)
                )
            except ValueError:
                raise AuditIntegrityError("Audit journal has an invalid head") from None

    async def read(self, scope: AuditScope, *, after_sequence: int = 0, limit: int = 100) -> tuple[JournalEntry, ...]:
        self._ready()
        self._bounds(after_sequence, limit)
        chain = self._identity(scope)
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        select(audit_entries)
                        .where(
                            audit_entries.c.chain_id == chain,
                            audit_entries.c.sequence > after_sequence,
                        )
                        .order_by(audit_entries.c.sequence)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return tuple(self._decode(row, scope) for row in rows)

    async def verify(self, scope: AuditScope, *, trusted_head: AuditHead | None = None) -> VerificationResult:
        try:
            result = await self._verify(scope, trusted_head=trusted_head)
        except BaseException:
            self._observe("verify", "failed")
            raise
        self._observe("verify", "verified")
        return result

    async def _verify(self, scope: AuditScope, *, trusted_head: AuditHead | None = None) -> VerificationResult:
        self._ready()
        chain = self._identity(scope)
        async with infrastructure_unit(self._target, read_only=True) as session:
            # One statement captures the head and physical extent at a single committed snapshot.
            # Later appends do not invalidate this immutable prefix under READ COMMITTED.
            boundary = (
                await session.execute(
                    select(
                        select(audit_heads.c.sequence)
                        .where(audit_heads.c.chain_id == chain)
                        .scalar_subquery()
                        .label("head_sequence"),
                        select(audit_heads.c.entry_hash)
                        .where(audit_heads.c.chain_id == chain)
                        .scalar_subquery()
                        .label("head_hash"),
                        select(func.count())
                        .select_from(audit_entries)
                        .where(audit_entries.c.chain_id == chain)
                        .scalar_subquery(),
                        select(func.max(audit_entries.c.sequence))
                        .where(audit_entries.c.chain_id == chain)
                        .scalar_subquery(),
                    )
                )
            ).one()
            try:
                head = AuditHead(
                    chain_id=chain,
                    sequence=boundary[0] if boundary[0] is not None else 0,
                    entry_hash=boundary[1] if boundary[1] is not None else genesis_hash(chain),
                )
            except ValueError:
                raise AuditIntegrityError("Audit journal has an invalid head") from None
            if boundary[2] != head.sequence or (boundary[3] or 0) != head.sequence:
                raise AuditIntegrityError("Audit journal extent does not match its head")
            if trusted_head is not None and (trusted_head.chain_id != chain or trusted_head.sequence > head.sequence):
                raise AuditIntegrityError("Trusted checkpoint is outside the local journal history")
            previous = genesis_hash(chain)
            cursor = 0
            if trusted_head is not None and trusted_head.sequence == 0 and trusted_head.entry_hash != previous:
                raise AuditIntegrityError("Trusted genesis checkpoint does not match")
            while cursor < head.sequence:
                rows = (
                    (
                        await session.execute(
                            select(audit_entries)
                            .where(
                                audit_entries.c.chain_id == chain,
                                audit_entries.c.sequence > cursor,
                                audit_entries.c.sequence <= head.sequence,
                            )
                            .order_by(audit_entries.c.sequence)
                            .limit(1000)
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    raise AuditIntegrityError("Audit journal has a sequence gap")
                for row in rows:
                    entry = self._decode(row, scope)
                    if entry.sequence != cursor + 1 or entry.previous_hash != previous:
                        raise AuditIntegrityError("Audit journal has a broken sequence or hash link")
                    cursor, previous = entry.sequence, entry.entry_hash
                    if (
                        trusted_head is not None
                        and cursor == trusted_head.sequence
                        and previous != trusted_head.entry_hash
                    ):
                        raise AuditIntegrityError("Audit journal disagrees with the trusted checkpoint")
            if previous != head.entry_hash:
                raise AuditIntegrityError("Audit journal head does not match its entries")
            return VerificationResult(head=head, anchored=trusted_head == head)

    async def export(
        self,
        scope: AuditScope,
        *,
        after_sequence: int = 0,
        through_sequence: int,
        limit: int = 100,
    ) -> bytes:
        self._bounds(after_sequence, limit)
        if type(through_sequence) is not int or not after_sequence <= through_sequence <= after_sequence + limit:
            raise ValueError("Audit export must declare a bounded complete range")
        async with infrastructure_unit(self._target, read_only=True):
            expected = through_sequence - after_sequence
            entries = await self.read(scope, after_sequence=after_sequence, limit=max(1, expected))
            if expected == 0:
                result = await self.verify(scope)
                if through_sequence > result.head.sequence:
                    raise AuditIntegrityError("Audit export range exceeds the journal head")
                return b""
            if len(entries) != expected:
                raise AuditIntegrityError("Audit export did not materialize its complete declared range")
            previous = entries[0].previous_hash
            for sequence, entry in enumerate(entries, start=after_sequence + 1):
                if entry.sequence != sequence or entry.previous_hash != previous:
                    raise AuditIntegrityError("Audit export has a sequence gap or broken hash link")
                previous = entry.entry_hash
            # Verify against these frozen bytes' terminal digest, then export the same values.
            # No fresh storage read after verification can silently shorten or replace the range.
            await self.verify(scope, trusted_head=entries[-1].head)
            return b"".join(entry.to_bytes() + b"\n" for entry in entries)
