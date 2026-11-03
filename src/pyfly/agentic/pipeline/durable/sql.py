# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Primary native Data CAS, lifecycle events and Audit in one transaction.

Every method joins an existing write unit. Returned mutations are provisional
until its outer unit commits. Expired running effects are never requeued.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.agentic.pipeline.checkpoint import CheckpointRecord
from pyfly.agentic.pipeline.durable.models import (
    DurableApproval,
    DurableBinding,
    DurableClaim,
    DurableConflict,
    DurableEvent,
    DurableEventKind,
    DurableRunEnvelope,
    DurableRunKey,
    DurableStateError,
    bounded_json,
    checkpoint_json,
)
from pyfly.agentic.pipeline.result import NodeResult
from pyfly.audit import AuditJournal
from pyfly.audit.models import AuditActor, AuditCorrelation, AuditEvent, AuditResource, Identifier
from pyfly.data.relational.framework_schema import agentic_durable_events as events_table
from pyfly.data.relational.framework_schema import agentic_durable_runs as runs
from pyfly.data.relational.framework_schema import ensure_tables
from pyfly.data.relational.sqlalchemy import SessionProvider, SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.data.transaction.context import current_state

T = TypeVar("T", bound=BaseModel)


def checked(model: type[T], value: T) -> T:
    try:
        if not isinstance(value, model):
            raise ValueError
        return model.model_validate(value)
    except (ValidationError, AttributeError, ValueError, TypeError):
        raise DurableStateError("Durable reference is invalid") from None


def digest(value: object) -> str:
    return hashlib.sha256(bounded_json(value).encode()).hexdigest()


class SqlDurableRunStore:
    def __init__(
        self,
        transaction_manager: SqlAlchemyTransactionManager,
        session_provider: SessionProvider,
        audit_journal: AuditJournal,
        *,
        create_tables: bool = False,
        max_bytes: int = 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transaction_manager = transaction_manager
        self.sessions = session_provider
        self.audit = audit_journal
        self._create, self._max_bytes = create_tables, max_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started = False
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise DurableStateError("Invalid durable payload budget")
        self._participant()

    def _participant(self) -> None:
        if getattr(self.audit, "transaction_manager", None) is not self.transaction_manager:
            raise DurableStateError("Durable state and audit require the same native Data participant")
        if self.transaction_manager.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise DurableStateError("Durable state supports SQLite and PostgreSQL only")

    async def start(self) -> None:
        self._participant()
        await ensure_tables(self.transaction_manager, runs, events_table, create=self._create)
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @asynccontextmanager
    async def _unit(self, *, write: bool = True) -> AsyncIterator[AsyncSession]:
        if not self._started:
            raise DurableStateError("Start the durable store before use")
        self._participant()
        try:
            async with TransactionTemplate(
                self.transaction_manager, read_only=not write, read_from_primary=True
            ).transaction() as unit:
                current_state().check_manager(self.transaction_manager)
                session = self.sessions.current(self.transaction_manager.datasource)
                if session is None or unit is None:
                    raise DurableStateError("Durable state requires a native Data unit")
                try:
                    yield session
                except BaseException:
                    unit.set_rollback_only("Durable state transition failed")
                    unit.redact_rollback_reason("Durable state transition failed")
                    raise
        except SQLAlchemyError:
            raise DurableStateError("Durable state persistence failed") from None

    def _key(self, key: DurableRunKey) -> str:
        return digest(checked(DurableRunKey, key).model_dump(mode="json"))

    def _serialized(self, envelope: DurableRunEnvelope) -> str:
        return bounded_json(envelope.model_dump(mode="json"), max_bytes=self._max_bytes)

    def _decode(self, row: RowMapping) -> DurableRunEnvelope:
        try:
            text = row["payload"]
            if type(text) is not str or len(text.encode()) > self._max_bytes:
                raise ValueError
            if hashlib.sha256(text.encode()).hexdigest() != row["payload_digest"]:
                raise ValueError
            value = DurableRunEnvelope.model_validate_json(text)
            if value.version != row["version"] or self._key(value.key) != row["key_digest"]:
                raise ValueError
            self._consistent(value)
            return value
        except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
            raise DurableStateError("Persisted durable state is invalid") from None

    def _consistent(self, value: DurableRunEnvelope) -> None:
        if value.status == "cancelled":
            if value.cancel_actor is None or value.lease is not None or value.generation not in {2, 3}:
                raise DurableStateError("Persisted durable cancellation is invalid")
        elif (
            value.cancel_actor is not None
            or value.generation not in {1, 2}
            or (value.generation == 2 and (value.task is None or value.task.decision != "approve"))
        ):
            raise DurableStateError("Persisted durable generation is invalid")
        if value.last_claim is not None:
            prior = value.last_claim
            if (
                prior.key != value.key
                or prior.binding != value.binding
                or prior.epoch > value.epoch
                or prior.generation > value.generation
            ):
                raise DurableStateError("Persisted durable claim history differs")
            if value.lease is not None and value.lease != prior:
                raise DurableStateError("Persisted durable lease differs from claim history")
        if value.checkpoint is not None:
            pause_position = value.binding.node_ids.index(value.binding.pause_node_id) + 1
            if value.checkpoint.sequence >= pause_position and value.task is None:
                raise DurableStateError("Persisted durable approval history is missing")
            if value.checkpoint.sequence > pause_position and value.generation != 2:
                raise DurableStateError("Persisted durable successor has no approval")
        if value.lease is not None:
            lease = value.lease
            if (
                value.status != "running"
                or lease.key != value.key
                or lease.binding != value.binding
                or lease.generation != value.generation
                or lease.epoch != value.epoch
            ):
                raise DurableStateError("Persisted durable lease is invalid")
        elif value.status == "running":
            raise DurableStateError("Persisted durable lease is missing")
        if value.checkpoint is not None:
            self._record(value, value.checkpoint, existing=True)
        if value.status == "waiting_approval" and (
            value.task is None
            or value.task.decision is not None
            or value.checkpoint is None
            or not value.checkpoint.paused
        ):
            raise DurableStateError("Persisted durable approval is invalid")
        if value.task is not None and (value.task.node_id != value.binding.pause_node_id or value.task.generation != 1):
            raise DurableStateError("Persisted durable task binding is invalid")

    async def _load(self, session: AsyncSession, key: DurableRunKey, binding: DurableBinding) -> DurableRunEnvelope:
        binding = checked(DurableBinding, binding)
        row = (await session.execute(select(runs).where(runs.c.key_digest == self._key(key)))).mappings().first()
        if row is None:
            raise DurableConflict("Durable run was not found")
        value = self._decode(row)
        if value.key != key or value.binding != binding:
            raise DurableConflict("Durable run binding differs")
        return value

    async def load(self, key: DurableRunKey, binding: DurableBinding) -> DurableRunEnvelope:
        async with self._unit(write=False) as session:
            return await self._load(session, key, binding)

    async def _publish(
        self,
        session: AsyncSession,
        previous: DurableRunEnvelope | None,
        value: DurableRunEnvelope,
        kind: DurableEventKind,
        actor: AuditActor,
    ) -> DurableRunEnvelope:
        value = checked(DurableRunEnvelope, value)
        self._consistent(value)
        payload = self._serialized(value)
        fields = {
            "key_digest": self._key(value.key),
            "version": value.version,
            "payload_digest": hashlib.sha256(payload.encode()).hexdigest(),
            "payload": payload,
        }
        if previous is None:
            await session.execute(insert(runs).values(**fields))
        else:
            changed = await session.execute(
                update(runs)
                .where(runs.c.key_digest == fields["key_digest"], runs.c.version == previous.version)
                .values(**fields)
            )
            if not isinstance(changed, CursorResult) or changed.rowcount != 1:
                raise DurableConflict("Durable state changed concurrently")
        event = DurableEvent(
            key=value.key,
            sequence=value.version,
            kind=kind,
            status=value.status,
            generation=value.generation,
            epoch=value.epoch,
            node_id=value.checkpoint.node_id if value.checkpoint else None,
            actor=actor,
            occurred_at=self._clock(),
        )
        await session.execute(
            insert(events_table).values(
                key_digest=fields["key_digest"],
                sequence=value.version,
                payload_digest=digest(event.model_dump(mode="json")),
                payload=bounded_json(event.model_dump(mode="json")),
            )
        )
        outcomes: dict[
            DurableEventKind,
            Literal[
                "admitted",
                "started",
                "observed",
                "approval_required",
                "denied",
                "succeeded",
                "failed",
                "unknown",
                "cancelled",
            ],
        ] = {
            "created": "admitted",
            "claimed": "started",
            "renewed": "observed",
            "checkpointed": "observed",
            "waiting_approval": "approval_required",
            "approved": "admitted",
            "rejected": "denied",
            "succeeded": "succeeded",
            "failed": "failed",
            "unknown": "unknown",
            "cancelled": "cancelled",
        }
        outcome = outcomes[kind]
        await self.audit.append(
            AuditEvent(
                event_id="durable." + digest({"key": value.key.model_dump(mode="json"), "version": value.version}),
                event_type="agentic.durable." + kind,
                scope=value.key.scope,
                actor=actor,
                resource=AuditResource(
                    kind="agentic_run",
                    resource_id=value.key.run_id,
                    revision=value.version,
                    digest=value.binding.graph_digest,
                ),
                occurred_at=event.occurred_at,
                outcome=outcome,
                correlation=AuditCorrelation(run_id=value.key.run_id, node_id=event.node_id, claim_epoch=value.epoch),
                references=(
                    AuditResource(
                        kind="agentic_plan",
                        resource_id=value.binding.definition_id,
                        revision=value.binding.definition_revision,
                        digest=value.binding.plan_digest,
                    ),
                ),
            )
        )
        return value

    @staticmethod
    def _next(value: DurableRunEnvelope, **changes: Any) -> DurableRunEnvelope:
        return value.model_copy(update={"version": value.version + 1, **changes})

    async def create(
        self, key: DurableRunKey, binding: DurableBinding, original_input: object, *, actor: AuditActor
    ) -> DurableRunEnvelope:
        key, binding, actor = checked(DurableRunKey, key), checked(DurableBinding, binding), checked(AuditActor, actor)
        original = json.loads(bounded_json(original_input, max_bytes=self._max_bytes))
        value = DurableRunEnvelope(key=key, binding=binding, original_input=original, initiator=actor)
        async with self._unit() as session:
            return await self._publish(session, None, value, "created", actor)

    @staticmethod
    def _ttl(seconds: int) -> None:
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise DurableStateError("Durable lease TTL must be bounded")

    def _live(self, value: DurableRunEnvelope, claim: DurableClaim, *, allow_expired: bool = False) -> None:
        lease = value.lease
        if (
            value.status != "running"
            or lease is None
            or lease.owner != claim.owner
            or lease.key != claim.key
            or lease.binding != claim.binding
            or lease.epoch != claim.epoch
            or lease.generation != claim.generation
            or (not allow_expired and lease.expires_at <= self._clock())
        ):
            raise DurableConflict("Durable claim is not current")

    async def claim(
        self,
        key: DurableRunKey,
        binding: DurableBinding,
        *,
        owner: str,
        expected_generation: int,
        ttl_seconds: int = 30,
    ) -> DurableClaim:
        self._ttl(ttl_seconds)
        try:
            owner = TypeAdapter(Identifier).validate_python(owner)
        except ValueError:
            raise DurableStateError("Invalid durable owner") from None
        async with self._unit() as session:
            value = await self._load(session, key, binding)
            if (
                type(expected_generation) is not int
                or value.status != "queued"
                or value.generation != expected_generation
            ):
                raise DurableConflict("Durable run is not queued for this generation")
            claim = DurableClaim(
                key=value.key,
                binding=value.binding,
                owner=owner,
                generation=value.generation,
                epoch=value.epoch + 1,
                expires_at=self._clock() + timedelta(seconds=ttl_seconds),
            )
            await self._publish(
                session,
                value,
                self._next(value, status="running", lease=claim, last_claim=claim, epoch=claim.epoch),
                "claimed",
                AuditActor(kind="service", subject_id=owner),
            )
            return claim

    async def validate_claim(self, claim: DurableClaim, *, node_id: str | None = None) -> DurableRunEnvelope:
        claim = checked(DurableClaim, claim)
        async with self._unit(write=False) as session:
            value = await self._load(session, claim.key, claim.binding)
            self._live(value, claim)
            if node_id is not None:
                completed = len(value.checkpoint.completed_nodes) if value.checkpoint else 0
                if completed >= len(value.binding.node_ids) or value.binding.node_ids[completed] != node_id:
                    raise DurableConflict("Durable node is not the admitted next cursor")
            return value

    async def renew(self, claim: DurableClaim, *, ttl_seconds: int = 30) -> DurableClaim:
        claim = checked(DurableClaim, claim)
        self._ttl(ttl_seconds)
        async with self._unit() as session:
            value = await self._load(session, claim.key, claim.binding)
            self._live(value, claim)
            current_lease = value.lease
            if current_lease is None:
                raise DurableConflict("Durable claim is not current")
            lease = current_lease.model_copy(update={"expires_at": self._clock() + timedelta(seconds=ttl_seconds)})
            await self._publish(
                session,
                value,
                self._next(value, lease=lease, last_claim=lease),
                "renewed",
                AuditActor(kind="service", subject_id=claim.owner),
            )
            return lease

    def _record(
        self, value: DurableRunEnvelope, record: CheckpointRecord, *, existing: bool = False
    ) -> CheckpointRecord:
        try:
            record = CheckpointRecord.model_validate(json.loads(checkpoint_json(record, max_bytes=self._max_bytes)))
            nodes = list(value.binding.node_ids[: record.sequence])
            previous = record.sequence - 1 if existing else (value.checkpoint.sequence if value.checkpoint else 0)
            if (
                record.sequence != previous + 1
                or record.sequence > len(value.binding.node_ids)
                or record.sequence < 1
                or record.completed_nodes != nodes
                or record.node_id != nodes[-1]
                or record.pipeline_name != value.binding.pipeline_name
                or record.run_id != value.key.run_id
                or record.execution_start is not None
                or bounded_json(record.state.get("inputs")) != bounded_json(value.original_input)
                or set(record.state) != {"inputs", "results", "shared_state"}
                or record.state["shared_state"] is not None
                or set(record.state["results"]) != set(nodes)
            ):
                raise ValueError
            for node, item in record.state["results"].items():
                if not isinstance(item, dict) or not {"success", "output"} <= set(item):
                    raise ValueError
                result = NodeResult.model_validate(item, strict=True)
                if result.node_id != node or result.success is not True or result.skipped:
                    raise ValueError
            is_pause = record.node_id == value.binding.pause_node_id
            if record.paused != is_pause or (
                is_pause
                and (
                    not (
                        value.generation == 1
                        or (existing and value.status == "cancelled")
                        or (
                            existing
                            and value.generation == 2
                            and value.task is not None
                            and value.task.decision == "approve"
                        )
                    )
                    or not isinstance(record.pause_reason, str)
                    or record.state["results"][record.node_id]["output"] != {"reason": record.pause_reason}
                )
            ):
                raise ValueError
            if not existing and value.checkpoint:
                for node in value.checkpoint.completed_nodes:
                    if bounded_json(value.checkpoint.state["results"][node]) != bounded_json(
                        record.state["results"][node]
                    ):
                        raise ValueError
                if is_pause and value.task is not None:
                    raise ValueError
            return record
        except (ValidationError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise DurableStateError("Durable checkpoint differs from the admitted linear cursor") from None

    async def checkpoint(self, claim: DurableClaim, record: CheckpointRecord) -> DurableRunEnvelope:
        claim = checked(DurableClaim, claim)
        record = CheckpointRecord.model_validate_json(checkpoint_json(record, max_bytes=self._max_bytes))
        async with self._unit() as session:
            value = await self._load(session, claim.key, claim.binding)
            if value.status == "waiting_approval" and value.last_claim is not None and value.checkpoint is not None:
                prior = value.last_claim
                if (prior.owner, prior.generation, prior.epoch) == (
                    claim.owner,
                    claim.generation,
                    claim.epoch,
                ) and checkpoint_json(record, max_bytes=self._max_bytes) == checkpoint_json(
                    value.checkpoint, max_bytes=self._max_bytes
                ):
                    return value
            self._live(value, claim)
            record = self._record(value, record)
            changes: dict[str, object] = {"checkpoint": record}
            kind: DurableEventKind = "checkpointed"
            if record.paused:
                changes.update(
                    status="waiting_approval",
                    lease=None,
                    task=DurableApproval(
                        task_id="approval."
                        + digest({"key": value.key.model_dump(mode="json"), "generation": value.generation}),
                        node_id=record.node_id,
                        generation=value.generation,
                    ),
                )
                kind = "waiting_approval"
            return await self._publish(
                session, value, self._next(value, **changes), kind, AuditActor(kind="service", subject_id=claim.owner)
            )

    async def checkpoint_for(self, claim: DurableClaim) -> CheckpointRecord | None:
        value = await self.validate_claim(claim)
        record = value.checkpoint
        if record is not None and record.paused:
            if value.task is None or value.task.decision != "approve" or value.generation != value.task.generation + 1:
                raise DurableConflict("Durable checkpoint has no admitted approval")
            return record.model_copy(update={"paused": False, "pause_reason": None})
        return record

    async def decide(
        self,
        key: DurableRunKey,
        binding: DurableBinding,
        *,
        task_id: str,
        expected_generation: int,
        decision: Literal["approve", "reject"],
        actor: AuditActor,
        idempotency_key: str,
        policy_digest: str,
    ) -> DurableRunEnvelope:
        actor = checked(AuditActor, actor)
        try:
            requested = DurableApproval(
                task_id=task_id,
                node_id=binding.pause_node_id,
                generation=expected_generation,
                decision=decision,
                actor=actor,
                idempotency_key=idempotency_key,
                policy_digest=policy_digest,
            )
        except (ValidationError, ValueError):
            raise DurableStateError("Durable decision is invalid") from None
        async with self._unit() as session:
            value = await self._load(session, key, binding)
            if actor.kind != "human" or actor.subject_id == value.initiator.subject_id:
                raise DurableConflict("Durable approval requires an independent human principal")
            if value.task == requested:
                return value
            if (
                value.status != "waiting_approval"
                or value.task is None
                or value.task.decision is not None
                or (value.task.task_id != requested.task_id or value.generation != requested.generation)
            ):
                raise DurableConflict("Durable approval generation is not current")
            status = "queued" if decision == "approve" else "rejected"
            return await self._publish(
                session,
                value,
                self._next(
                    value,
                    task=requested,
                    status=status,
                    generation=value.generation + 1 if decision == "approve" else value.generation,
                ),
                "approved" if decision == "approve" else "rejected",
                actor,
            )

    async def finish(
        self, claim: DurableClaim, *, outcome: Literal["succeeded", "failed", "unknown"]
    ) -> DurableRunEnvelope:
        claim = checked(DurableClaim, claim)
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise DurableStateError("Invalid durable terminal outcome")
        async with self._unit() as session:
            value = await self._load(session, claim.key, claim.binding)
            self._live(value, claim, allow_expired=outcome == "unknown")
            if outcome == "succeeded" and (
                value.checkpoint is None or value.checkpoint.completed_nodes != list(value.binding.node_ids)
            ):
                raise DurableConflict("Durable success requires every admitted checkpoint")
            return await self._publish(
                session,
                value,
                self._next(value, status=outcome, lease=None),
                outcome,
                AuditActor(kind="service", subject_id=claim.owner),
            )

    async def cancel(
        self, key: DurableRunKey, binding: DurableBinding, *, actor: AuditActor, expected_version: int
    ) -> DurableRunEnvelope:
        actor = checked(AuditActor, actor)
        async with self._unit() as session:
            value = await self._load(session, key, binding)
            if (
                value.status == "cancelled"
                and value.cancel_actor == actor
                and type(expected_version) is int
                and expected_version == value.version - 1
            ):
                return value
            if (
                value.status not in {"queued", "waiting_approval"}
                or type(expected_version) is not int
                or value.version != expected_version
            ):
                raise DurableConflict("Durable run cannot be cancelled at this version")
            return await self._publish(
                session,
                value,
                self._next(
                    value,
                    status="cancelled",
                    cancel_actor=actor,
                    generation=value.generation + 1,
                    epoch=value.epoch + 1,
                    lease=None,
                ),
                "cancelled",
                actor,
            )

    async def events(
        self, key: DurableRunKey, binding: DurableBinding, *, after: int = 0, limit: int = 100
    ) -> tuple[DurableEvent, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DurableStateError("Invalid durable event window")
        async with self._unit(write=False) as session:
            value = await self._load(session, key, binding)
            if after > value.version:
                raise DurableConflict("Durable event cursor is beyond current state")
            rows = (
                (
                    await session.execute(
                        select(events_table)
                        .where(events_table.c.key_digest == self._key(key), events_table.c.sequence > after)
                        .order_by(events_table.c.sequence)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            try:
                result = tuple(DurableEvent.model_validate_json(row["payload"]) for row in rows)
                if len(result) != min(limit, value.version - after):
                    raise ValueError
                for expected, (event, row) in enumerate(zip(result, rows, strict=True), start=after + 1):
                    if (
                        event.key != value.key
                        or event.sequence != row["sequence"]
                        or event.sequence != expected
                        or hashlib.sha256(row["payload"].encode()).hexdigest() != row["payload_digest"]
                    ):
                        raise ValueError
                return result
            except (ValidationError, ValueError, TypeError, KeyError):
                raise DurableStateError("Persisted durable events are invalid") from None
