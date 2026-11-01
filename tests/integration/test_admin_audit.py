# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Authoritative scoped read-only Admin access to committed native journal facts."""

import importlib

import pytest
from pydantic import ValidationError

from pyfly.audit import AuditScope
from pyfly.audit.ports import AuditError
from pyfly.data.transaction import Propagation, TransactionTemplate, infrastructure_unit, resolve_manager
from pyfly.kernel.exceptions import ForbiddenException, UnauthorizedException, ValidationException
from pyfly.security.context import SecurityContext
from tests.integration.test_audit_journal import event, journal
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")
SCOPE = event().scope
OTHER = AuditScope(tenant_id="local", workspace_id="studio", project_id="other")
USER = SecurityContext(user_id="verified-reader", roles=["ADMIN"])


class Resolver:
    revoked = False

    async def resolve(self, security, requested_handle):
        if self.revoked or security.user_id != USER.user_id or requested_handle not in (None, "mine"):
            raise ForbiddenException("fixture private authority detail")
        return SCOPE


class Permissions:
    allowed = True

    def has_permission(self, context, target, permission, *, target_type=None):
        return self.allowed and target == SCOPE and permission == "admin:audit:read"


async def provider(backend):
    api = importlib.import_module("pyfly.admin.audit")
    instance = await journal(backend)
    resolver, permissions = Resolver(), Permissions()
    return api.AdminAuditProvider(instance, resolver, permissions), instance, resolver, permissions


async def test_cursor_pages_are_scoped_bounded_and_do_not_append(relational_backend: RelationalBackend):
    reader, instance, _, _ = await provider(relational_backend)
    async with infrastructure_unit(instance.engine):
        for index in range(3):
            await instance.append(event(f"e-{index}"))
        await instance.append(event("other").model_copy(update={"scope": OTHER}))
    first = await reader.page(USER, scope_handle="mine", limit=2)
    assert [row.sequence for row in first.entries] == [1, 2]
    assert first.has_more and first.snapshot_through_sequence == 3
    async with infrastructure_unit(instance.engine):
        await instance.append(event("new-after-page"))
    last = await reader.page(USER, after_sequence=first.next_after_sequence, through_sequence=3, limit=2)
    assert [row.sequence for row in last.entries] == [3]
    assert not last.has_more and last.verification == "not_verified"
    assert len(await instance.read(SCOPE)) == 4
    assert len(await instance.outbox.pending("pyfly.audit.export")) == 5
    with pytest.raises(ValidationError):
        first.has_more = False


async def test_current_authority_is_required_for_every_page(relational_backend: RelationalBackend):
    reader, _, resolver, permissions = await provider(relational_backend)
    with pytest.raises(UnauthorizedException):
        await reader.page(SecurityContext.anonymous())
    with pytest.raises(ForbiddenException):
        await reader.page(SecurityContext(user_id=USER.user_id, roles=["USER"]))
    with pytest.raises(ForbiddenException) as denied:
        await reader.page(USER, scope_handle="other")
    assert "fixture private" not in str(denied.value)
    assert (await reader.page(USER)).head.sequence == 0
    permissions.allowed = False
    with pytest.raises(ForbiddenException):
        await reader.page(USER)
    permissions.allowed = True
    resolver.revoked = True
    with pytest.raises(ForbiddenException):
        await reader.page(USER)


@pytest.mark.parametrize(
    "values", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"after_sequence": -1}, {"through_sequence": True}]
)
async def test_query_bounds_never_coerce(relational_backend: RelationalBackend, values):
    reader, _, _, _ = await provider(relational_backend)
    with pytest.raises(ValidationException):
        await reader.page(USER, **values)


async def test_revocation_during_read_never_releases_snapshot(relational_backend: RelationalBackend):
    reader, instance, resolver, _ = await provider(relational_backend)
    original = instance.read

    async def read(*args, **kwargs):
        result = await original(*args, **kwargs)
        resolver.revoked = True
        return result

    instance.read = read
    with pytest.raises(ForbiddenException):
        await reader.page(USER)


async def test_verify_requires_separate_permission_and_reports_only_bounded_unanchored_range(relational_backend):
    reader, instance, _, permissions = await provider(relational_backend)
    async with infrastructure_unit(instance.engine):
        for index in range(4):
            await instance.append(event(f"verify-{index}"))
    assert not (await reader.page(USER)).can_verify
    with pytest.raises(ForbiddenException):
        await reader.verify_range(USER, first_sequence=2, last_sequence=3)
    permissions.has_permission = lambda *args, **kwargs: True
    page = await reader.page(USER)
    assert page.can_verify
    result = await reader.verify_range(USER, first_sequence=2, last_sequence=3)
    assert result.verification == "range_verified_unanchored"
    assert result.previous_hash == (await instance.read(SCOPE, limit=1))[0].entry_hash
    assert len(await instance.read(SCOPE)) == 4
    with pytest.raises(ValidationException):
        await reader.verify_range(USER, first_sequence=1, last_sequence=101)


async def test_cross_scope_or_adapter_error_never_releases_facts_or_exception_details(relational_backend):
    reader, instance, _, _ = await provider(relational_backend)
    async with infrastructure_unit(instance.engine):
        await instance.append(event("ours"))
        foreign = await instance.append(event("foreign").model_copy(update={"scope": OTHER}))

    async def foreign_read(*args, **kwargs):
        return (foreign,)

    instance.read = foreign_read
    with pytest.raises(AuditError, match="Audit read is unavailable"):
        await reader.page(USER)

    async def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_CONNECTION_STRING_CANARY")

    instance.read = broken
    with pytest.raises(AuditError) as failed:
        await reader.page(USER)
    assert "PRIVATE" not in str(failed.value)


@pytest.mark.parametrize("mode", ["write", "suspended", "auto_read"])
async def test_admin_reader_never_exposes_uncommitted_or_suspended_business_units(relational_backend, mode):
    from pyfly.data.transaction.template import AutoUnit

    reader, instance, _, _ = await provider(relational_backend)
    manager = resolve_manager(instance.engine)
    if mode == "auto_read":
        async with AutoUnit(manager, read_only=True, autocommit=True):
            with pytest.raises(AuditError, match="committed"):
                await reader.page(USER)
    else:
        async with TransactionTemplate(manager).transaction():
            await instance.append(event("not-yet-committed"))
            if mode == "suspended":
                async with TransactionTemplate(manager).transaction(propagation=Propagation.NOT_SUPPORTED):
                    with pytest.raises(AuditError, match="committed"):
                        await reader.page(USER)
            else:
                with pytest.raises(AuditError, match="committed"):
                    await reader.page(USER)
