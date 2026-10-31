# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Shared initialization metadata on native primary-authoritative transactions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_hex
from typing import Any

from pyfly.mcp.sessions import McpSessionMetadata, McpSessionPrincipal


class SqlMcpSessionStore:
    """A shared SQL adapter using the context's named/native datasource.

    Operations own short primary transactions, never an application's business
    unit. Capacity is serialized by a resource row; conditional updates never
    recreate a deleted or expired session. Schema creation must be explicitly
    disabled when migrations own the schema.
    """

    def __init__(
        self, datasource: object, *, create_table: bool = True, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.datasource, self.create_table = datasource, create_table
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started = False
        self._guard = asyncio.Lock()

    @staticmethod
    def _tables() -> tuple[Any, Any]:
        from pyfly.data.relational.framework_schema import mcp_session_admission, mcp_sessions

        return mcp_sessions, mcp_session_admission

    async def start(self) -> None:
        if not self._started:
            from pyfly.data.relational.framework_schema import ensure_tables

            async with self._guard:
                if not self._started:
                    await ensure_tables(self.datasource, *self._tables(), create=self.create_table)
                    self._started = True

    async def stop(self) -> None:
        """Datasource lifetime belongs to its registry; metadata survives process exit."""

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[Any]:
        from pyfly.data.transaction import Propagation, TransactionTemplate

        await self.start()
        async with TransactionTemplate(
            self.datasource, propagation=Propagation.REQUIRES_NEW, read_from_primary=True
        ).transaction() as unit:
            assert unit is not None
            yield unit.resource

    @staticmethod
    def _hash(value: str) -> str:
        return sha256(value.encode()).hexdigest()

    def _where(self, table: Any, identifier: str, principal: McpSessionPrincipal) -> tuple[Any, ...]:
        return (
            table.c.session_id == identifier,
            table.c.owner_hash == self._hash(principal.owner),
            table.c.resource_hash == self._hash(principal.resource),
            table.c.schema_version == 1,
            table.c.expires_at > self._clock(),
            table.c.credential_expires_at > self._clock(),
        )

    async def create(
        self,
        principal: McpSessionPrincipal,
        protocol_version: str,
        client_params: dict[str, Any],
        *,
        ttl: int,
        max_sessions: int,
    ) -> str | None:
        from sqlalchemy import delete, func, insert, select, update

        from pyfly.data.relational.upsert import insert_if_absent

        if ttl <= 0 or max_sessions <= 0:
            raise ValueError("MCP session TTL and capacity must be positive")
        now = self._clock()
        if principal.expires_at <= now:
            return None
        sessions, admission = self._tables()
        resource = self._hash(principal.resource)
        # Separate first-row insertion follows the native session-registry lock pattern.
        async with self._unit() as unit:
            await insert_if_absent(unit, admission, {"resource_hash": resource, "version": 0}, key=["resource_hash"])
        async with self._unit() as unit:
            await unit.execute(
                update(admission).where(admission.c.resource_hash == resource).values(version=admission.c.version + 1)
            )
            await unit.execute(
                delete(sessions).where(sessions.c.resource_hash == resource, sessions.c.expires_at <= now)
            )
            count = await unit.scalar(
                select(func.count()).select_from(sessions).where(sessions.c.resource_hash == resource)
            )
            if count >= max_sessions:
                return None
            identifier = token_hex(32)
            await unit.execute(
                insert(sessions).values(
                    session_id=identifier,
                    resource_hash=resource,
                    owner_hash=self._hash(principal.owner),
                    protocol_version=protocol_version,
                    client_params=json.dumps(client_params, separators=(",", ":")),
                    initialized=False,
                    schema_version=1,
                    revision=0,
                    credential_expires_at=principal.expires_at,
                    expires_at=min(now + timedelta(seconds=ttl), principal.expires_at),
                )
            )
            return identifier

    async def get(self, identifier: str, principal: McpSessionPrincipal, *, ttl: int) -> McpSessionMetadata | None:
        from sqlalchemy import select, update

        if principal.expires_at <= self._clock():
            return None
        table, _ = self._tables()
        async with self._unit() as unit:
            row = (
                (await unit.execute(select(table).where(*self._where(table, identifier, principal)).with_for_update()))
                .mappings()
                .first()
            )
            if row is None:
                return None
            try:
                params = json.loads(row["client_params"])
                if not isinstance(params, dict):
                    return None
            except (ValueError, TypeError):
                return None
            expires = min(self._clock() + timedelta(seconds=ttl), row["credential_expires_at"], principal.expires_at)
            touched = await unit.execute(
                update(table)
                .where(*self._where(table, identifier, principal))
                .values(expires_at=expires, revision=table.c.revision + 1)
            )
            if touched.rowcount != 1:
                return None
            return McpSessionMetadata(
                identifier,
                principal.owner,
                principal.resource,
                row["protocol_version"],
                params,
                expires,
                row["credential_expires_at"],
                row["initialized"],
                row["schema_version"],
            )

    async def initialize(self, identifier: str, principal: McpSessionPrincipal) -> bool:
        from sqlalchemy import update

        if principal.expires_at <= self._clock():
            return False
        table, _ = self._tables()
        async with self._unit() as unit:
            result = await unit.execute(
                update(table)
                .where(*self._where(table, identifier, principal))
                .values(initialized=True, revision=table.c.revision + 1)
            )
            return bool(result.rowcount == 1)

    async def delete(self, identifier: str, principal: McpSessionPrincipal) -> bool:
        from sqlalchemy import delete

        if principal.expires_at <= self._clock():
            return False
        table, _ = self._tables()
        async with self._unit() as unit:
            result = await unit.execute(delete(table).where(*self._where(table, identifier, principal)))
            return bool(result.rowcount == 1)
