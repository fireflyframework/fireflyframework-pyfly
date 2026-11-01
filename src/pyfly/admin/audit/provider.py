# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Current-authority native journal reads; never writes, starts or closes borrowed ports."""

from __future__ import annotations

import asyncio

from pyfly.admin.audit.models import AdminAuditPage, AdminAuditRange
from pyfly.admin.audit.ports import AdminAuditScopeResolver
from pyfly.audit._infrastructure import has_active_data_units
from pyfly.audit.models import AuditHead, AuditScope, JournalEntry
from pyfly.audit.ports import AuditError, AuditJournal
from pyfly.kernel.exceptions import ForbiddenException, UnauthorizedException, ValidationException
from pyfly.security.context import SecurityContext
from pyfly.security.permission import PermissionEvaluator


class AdminAuditProvider:
    def __init__(
        self,
        journal: AuditJournal,
        resolver: AdminAuditScopeResolver,
        evaluator: PermissionEvaluator,
        *,
        allowed_roles: tuple[str, ...] = ("ADMIN",),
    ) -> None:
        self._journal = journal
        self._resolver = resolver
        self._evaluator = evaluator
        self._roles = allowed_roles

    async def _authorize(self, security: SecurityContext, handle: str | None, permission: str) -> AuditScope:
        if not isinstance(security, SecurityContext) or not security.is_authenticated:
            raise UnauthorizedException("Authentication required")
        if not self._roles or not security.has_any_role(list(self._roles)):
            raise ForbiddenException("Audit access is forbidden")
        try:
            if handle is not None and (not isinstance(handle, str) or not 1 <= len(handle) <= 128):
                raise ValueError
            scope = AuditScope.model_validate(await self._resolver.resolve(security, handle))
            if self._evaluator.has_permission(security, scope, permission, target_type="AuditScope") is not True:
                raise ValueError
            return scope
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except Exception:
            raise ForbiddenException("Audit access is forbidden") from None

    @staticmethod
    def _count(value: int, maximum: int = 2**63 - 1) -> None:
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValidationException("Invalid audit cursor or page bounds")

    @staticmethod
    def _committed() -> None:
        if has_active_data_units():
            raise AuditError("Audit administration requires committed facts outside all active Data units")

    async def page(
        self,
        security: SecurityContext,
        *,
        scope_handle: str | None = None,
        after_sequence: int = 0,
        through_sequence: int | None = None,
        limit: int = 25,
    ) -> AdminAuditPage:
        scope = await self._authorize(security, scope_handle, "admin:audit:read")
        self._committed()
        self._count(after_sequence)
        self._count(limit, 100)
        if not limit:
            raise ValidationException("Invalid audit cursor or page bounds")
        if through_sequence is not None:
            self._count(through_sequence)
        try:
            head = AuditHead.model_validate(await self._journal.head(scope))
            through = head.sequence if through_sequence is None else through_sequence
            if not after_sequence <= through <= head.sequence:
                raise ValidationException("Invalid audit cursor or page bounds")
            rows = await self._journal.read(scope, after_sequence=after_sequence, limit=limit)
            entries = tuple(JournalEntry.model_validate(row) for row in rows if row.sequence <= through)
            if len(rows) > limit or any(
                row.event.scope != scope or row.chain_id != head.chain_id or row.sequence != after_sequence + index + 1
                for index, row in enumerate(entries)
            ):
                raise ValueError
            next_after = entries[-1].sequence if entries else after_sequence
            if next_after < through and len(entries) < limit:
                raise ValueError
            if await self._authorize(security, scope_handle, "admin:audit:read") != scope:
                raise ForbiddenException("Audit access is forbidden")
            self._committed()
            can_verify = (
                self._evaluator.has_permission(security, scope, "admin:audit:verify", target_type="AuditScope") is True
            )
            return AdminAuditPage(
                scope=scope,
                head=head,
                entries=entries,
                snapshot_through_sequence=through,
                next_after_sequence=next_after,
                has_more=next_after < through,
                can_verify=can_verify,
            )
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except (ForbiddenException, ValidationException):
            raise
        except Exception:
            raise AuditError("Audit read is unavailable") from None

    async def verify_range(
        self, security: SecurityContext, *, scope_handle: str | None = None, first_sequence: int, last_sequence: int
    ) -> AdminAuditRange:
        scope = await self._authorize(security, scope_handle, "admin:audit:verify")
        self._committed()
        self._count(first_sequence)
        self._count(last_sequence)
        if not 1 <= first_sequence <= last_sequence or last_sequence - first_sequence >= 100:
            raise ValidationException("Invalid bounded audit range")
        try:
            from pyfly.audit.archive import build_archive_segment

            segment = await build_archive_segment(
                self._journal, scope, first_sequence=first_sequence, last_sequence=last_sequence
            )
            if await self._authorize(security, scope_handle, "admin:audit:verify") != scope:
                raise ForbiddenException("Audit access is forbidden")
            return AdminAuditRange(
                scope=scope,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
                previous_hash=segment.manifest.previous_hash,
                terminal_hash=segment.manifest.terminal_hash,
            )
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except ForbiddenException:
            raise
        except Exception:
            raise AuditError("Audit range verification is unavailable") from None
