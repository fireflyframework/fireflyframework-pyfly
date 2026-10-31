# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Migration-owner DDL for audit mutation guards. Privileged owners can still disable these guards."""

from __future__ import annotations

from sqlalchemy import text

from pyfly.audit.ports import AuditError
from pyfly.data.transaction import infrastructure_unit


def mutation_guard_ddl(dialect: str) -> tuple[str, ...]:
    """DDL for a migration owner; only fixed framework identifiers are interpolated."""
    if dialect == "sqlite":
        return tuple(
            f"CREATE TRIGGER IF NOT EXISTS pyfly_audit_entries_no_{operation.lower()} "
            f"BEFORE {operation} ON pyfly_audit_entries BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
            for operation in ("UPDATE", "DELETE")
        )
    if dialect == "postgresql":
        return (
            "CREATE OR REPLACE FUNCTION pyfly_audit_reject_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit is append-only'; END; $$",
            "CREATE OR REPLACE TRIGGER pyfly_audit_entries_no_mutation BEFORE UPDATE OR DELETE "
            "ON pyfly_audit_entries FOR EACH ROW EXECUTE FUNCTION pyfly_audit_reject_mutation()",
            "CREATE OR REPLACE TRIGGER pyfly_audit_entries_no_truncate BEFORE TRUNCATE "
            "ON pyfly_audit_entries FOR EACH STATEMENT EXECUTE FUNCTION pyfly_audit_reject_mutation()",
        )
    raise AuditError("Audit journal supports SQLite and PostgreSQL only")


async def ensure_mutation_guards(datasource: object, dialect: str, *, create: bool) -> None:
    async with infrastructure_unit(datasource) as session:
        if create:
            for statement in mutation_guard_ddl(dialect):
                await session.execute(text(statement))
        if dialect == "sqlite":
            rows = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='pyfly_audit_entries'")
            )
            required = {"pyfly_audit_entries_no_update", "pyfly_audit_entries_no_delete"}
        else:
            rows = await session.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid='pyfly_audit_entries'::regclass "
                    "AND tgenabled IN ('O','A') AND NOT tgisinternal"
                )
            )
            required = {"pyfly_audit_entries_no_mutation", "pyfly_audit_entries_no_truncate"}
        if not required.issubset({row[0] for row in rows}):
            raise AuditError("Audit mutation guards are missing; apply the audit schema migration")
