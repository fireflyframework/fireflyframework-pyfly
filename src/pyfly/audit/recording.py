# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Mandatory semantic recording; native transaction managers own every commit."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from pyfly.audit.models import AuditEvent, AuditResource, AuditUsage, Fact, Identifier, JournalEntry
from pyfly.audit.ports import AuditAdmissionJournal, AuditAdmissionRefused, AuditError, AuditJournal
from pyfly.audit.recording_context import AuditContext, AuditContextResolver, AuditOperation

if TYPE_CHECKING:
    from pyfly.data.transaction import TransactionManager

type Outcome = Literal[
    "intent",
    "admitted",
    "denied",
    "approval_required",
    "started",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
    "reconciled",
    "observed",
]


class AuditEffectUncertain(AuditError):
    retryable = False


class _Enrichment(Fact):
    usage: AuditUsage | None = None
    references: tuple[AuditResource, ...] = Field(default=(), max_length=16)


class AuditEffect:
    __slots__ = ("_intent", "_enrichment")

    def __init__(self, intent: JournalEntry | None) -> None:
        self._intent = intent
        self._enrichment = _Enrichment()

    @property
    def intent(self) -> JournalEntry | None:
        return self._intent

    def enrich(self, *, usage: AuditUsage | None = None, references: tuple[AuditResource, ...] = ()) -> None:
        self._enrichment = _Enrichment(
            usage=usage or self._enrichment.usage, references=(*self._enrichment.references, *references)
        )


class AuditRecorder:
    def __init__(
        self,
        journal: AuditJournal,
        resolver: AuditContextResolver,
        *,
        transaction_manager: TransactionManager,
        modules: frozenset[str],
    ) -> None:
        try:
            modules = TypeAdapter(frozenset[Identifier]).validate_python(modules)
        except ValueError:
            raise AuditError("Audit recording requires bounded explicit module identifiers") from None
        if not modules or transaction_manager.capabilities.backend not in {"sqlite", "postgresql"}:
            raise AuditError("Audit recording requires explicit modules and a supported native SQL datasource")
        if not isinstance(resolver, AuditContextResolver):
            raise AuditError("Audit recording requires a trusted host context resolver")
        self.journal, self.resolver, self.manager = journal, resolver, transaction_manager
        self.modules = frozenset(modules)

    def enabled(self, module: str) -> bool:
        return module in self.modules

    @staticmethod
    def _poison_active() -> None:
        from pyfly.data.transaction.context import current_state

        state = current_state()
        seen: set[int] = set()
        for name, _ in (*state.units, *state.scopes):
            for unit in state.held_units(name):
                if id(unit) not in seen and not unit.completed:
                    unit.set_rollback_only("Mandatory semantic audit failed")
                    unit.redact_rollback_reason("Mandatory semantic audit failed")
                    seen.add(id(unit))

    async def _context(self, operation: AuditOperation) -> AuditContext:
        try:
            operation = AuditOperation.model_validate(operation)
            context = AuditContext.model_validate(await self.resolver.resolve(operation))
        except asyncio.CancelledError:
            raise asyncio.CancelledError() from None
        except Exception:
            raise AuditError("Trusted audit context is unavailable") from None
        if context.correlation.operation_id is None:
            context = context.model_copy(
                update={"correlation": context.correlation.model_copy(update={"operation_id": str(uuid4())})}
            )
        return context

    @staticmethod
    def _event(operation: AuditOperation, context: AuditContext, outcome: Outcome) -> AuditEvent:
        return AuditEvent(
            event_id=str(uuid4()),
            event_type=f"{operation.module}.{operation.action}",
            scope=context.scope,
            actor=context.actor,
            resource=context.resource,
            occurred_at=datetime.now(UTC),
            outcome=outcome,
            correlation=context.correlation,
            references=context.references,
            reason_codes=context.reason_codes,
            usage=context.usage,
        )

    async def state(self, operation: AuditOperation, *, outcome: Outcome = "admitted") -> JournalEntry | None:
        from pyfly.data.transaction import current_unit_of_work

        try:
            operation = AuditOperation.model_validate(operation)
            if not self.enabled(operation.module):
                return None
            unit = current_unit_of_work(self.manager.datasource)
            if unit is None or unit.manager is not self.manager or unit.completed or unit.read_only or unit.autocommit:
                raise AuditError("Semantic state audit requires the same active native SQL write unit")
            context = await self._context(operation)
            return await self.journal.append(self._event(operation, context, outcome))
        except BaseException as error:
            self._poison_active()
            if isinstance(error, asyncio.CancelledError):
                raise asyncio.CancelledError() from None
            if isinstance(error, AuditError) or not isinstance(error, Exception):
                raise
            raise AuditError("Trusted semantic audit context or state recording is unavailable") from None

    @staticmethod
    def _outside_units() -> None:
        from pyfly.audit._infrastructure import has_active_data_units

        if has_active_data_units():
            raise AuditError(
                "Independent audit and external effects require no open Data unit; "
                "dispatch from an outbox or after the business unit exits"
            )

    async def _independent(self, event: AuditEvent, *, require_new: bool = False) -> JournalEntry:
        from pyfly.data.transaction import TransactionTemplate

        self._outside_units()
        if require_new and not isinstance(self.journal, AuditAdmissionJournal):
            raise AuditAdmissionRefused("This journal does not support durable effect admission")
        try:
            async with TransactionTemplate(self.manager).transaction():
                if require_new:
                    assert isinstance(self.journal, AuditAdmissionJournal)
                    entry = await self.journal.admit_once(event)
                else:
                    entry = await self.journal.append(event)
            return entry
        except asyncio.CancelledError:
            raise asyncio.CancelledError() from None
        except AuditError:
            raise
        except Exception as error:
            if getattr(error, "retryable", True) is False:
                raise AuditEffectUncertain(
                    "Audit commit was not confirmed; no external effect was admitted; do not retry automatically"
                ) from None
            raise AuditError("Independent audit recording could not be confirmed") from None

    async def observation(self, operation: AuditOperation, *, outcome: Outcome = "observed") -> JournalEntry | None:
        try:
            operation = AuditOperation.model_validate(operation)
            if not self.enabled(operation.module):
                return None
            self._outside_units()
            context = await self._context(operation)
            return await self._independent(self._event(operation, context, outcome))
        except asyncio.CancelledError:
            raise asyncio.CancelledError() from None
        except AuditError:
            raise
        except Exception:
            raise AuditError("Trusted semantic audit context or observation is unavailable") from None

    async def _outcome(
        self, operation: AuditOperation, context: AuditContext, effect: AuditEffect, outcome: Outcome
    ) -> None:
        assert effect.intent is not None
        enriched = AuditContext.model_validate(
            context.model_copy(
                update={
                    "correlation": context.correlation.model_copy(
                        update={"causation_event_id": effect.intent.event.event_id}
                    ),
                    "usage": effect._enrichment.usage or context.usage,
                    "references": (*context.references, *effect._enrichment.references),
                }
            )
        )
        await self._independent(self._event(operation, enriched, outcome))

    @asynccontextmanager
    async def effect(
        self, operation: AuditOperation, *, admission_key: str | None = None
    ) -> AsyncIterator[AuditEffect]:
        try:
            operation = AuditOperation.model_validate(operation)
            if admission_key is not None:
                admission_key = TypeAdapter(Identifier).validate_python(admission_key)
        except ValueError:
            raise AuditError("A bounded semantic audit operation is required") from None
        if not self.enabled(operation.module):
            yield AuditEffect(None)
            return
        self._outside_units()
        try:
            context = await self._context(operation)
            event = self._event(operation, context, "intent")
            if admission_key is not None:
                identity = json.dumps(
                    {
                        "scope": context.scope.model_dump(mode="json"),
                        "module": operation.module,
                        "action": operation.action,
                        "command": admission_key,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                event = event.model_copy(update={"event_id": "intent." + hashlib.sha256(identity.encode()).hexdigest()})
            intent = await self._independent(event, require_new=admission_key is not None)
        except AuditError:
            raise
        except Exception:
            raise AuditError("Trusted audit context or committed effect intent is unavailable") from None
        effect = AuditEffect(intent)
        from pyfly.audit._effects import admitted_audit_effect
        from pyfly.data.transaction.template import run_shielded

        try:
            with admitted_audit_effect(self.journal):
                yield effect
        except asyncio.CancelledError:
            await run_shielded(self._outcome(operation, context, effect, "unknown"))
            raise asyncio.CancelledError() from None
        except Exception:
            await run_shielded(self._outcome(operation, context, effect, "unknown"))
            raise AuditEffectUncertain(
                "The effect may have occurred; its committed intent is unknown; do not retry automatically"
            ) from None
        else:
            _, error, cancelled = await run_shielded(self._outcome(operation, context, effect, "observed"))
            if cancelled:
                await run_shielded(self._outcome(operation, context, effect, "unknown"))
                raise asyncio.CancelledError()
            if error is not None:
                raise AuditEffectUncertain(
                    "The effect may have occurred; its outcome could not be retained; do not retry automatically"
                ) from None
