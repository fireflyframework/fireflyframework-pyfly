# Copyright 2026 Firefly Software Foundation.
# SPDX-License-Identifier: Apache-2.0
"""Portable initialization metadata, never transport objects or bearer tokens."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class McpSessionPrincipal:
    """Host-verified credential binding and canonical resource, refreshed on every request."""

    owner: str
    resource: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner or len(self.owner) > 512 or not self.resource or len(self.resource) > 2048:
            raise ValueError("MCP session requires a bounded verified owner and resource")
        if self.expires_at.tzinfo is None:
            raise ValueError("MCP credential expiry must be timezone aware")


@dataclass(frozen=True)
class McpSessionMetadata:
    id: str
    owner: str
    resource: str
    protocol_version: str
    client_params: dict[str, Any]
    expires_at: datetime
    credential_expires_at: datetime
    initialized: bool = False
    schema_version: int = 1


@runtime_checkable
class McpSessionStore(Protocol):
    """Atomic admission, lookup/touch, initialization and deletion across replicas."""

    async def create(
        self,
        principal: McpSessionPrincipal,
        protocol_version: str,
        client_params: dict[str, Any],
        *,
        ttl: int,
        max_sessions: int,
    ) -> str | None: ...

    async def get(self, identifier: str, principal: McpSessionPrincipal, *, ttl: int) -> McpSessionMetadata | None: ...

    async def initialize(self, identifier: str, principal: McpSessionPrincipal) -> bool: ...

    async def delete(self, identifier: str, principal: McpSessionPrincipal) -> bool: ...


class InMemoryMcpSessionStore:
    """Single-process adapter; inject a shared SQL store for multiple processes."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rows: dict[str, McpSessionMetadata] = {}
        self._lock = asyncio.Lock()

    def _valid(self, row: McpSessionMetadata | None, principal: McpSessionPrincipal) -> bool:
        now = self._clock()
        return bool(
            row
            and row.schema_version == 1
            and row.owner == principal.owner
            and row.resource == principal.resource
            and row.expires_at > now
            and row.credential_expires_at > now
            and principal.expires_at > now
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
        if ttl <= 0 or max_sessions <= 0:
            raise ValueError("MCP session TTL and capacity must be positive")
        async with self._lock:
            now = self._clock()
            self._rows = {key: row for key, row in self._rows.items() if row.expires_at > now}
            if (
                principal.expires_at <= now
                or sum(row.resource == principal.resource for row in self._rows.values()) >= max_sessions
            ):
                return None
            identifier = token_hex(32)
            self._rows[identifier] = McpSessionMetadata(
                identifier,
                principal.owner,
                principal.resource,
                protocol_version,
                deepcopy(client_params),
                min(now + timedelta(seconds=ttl), principal.expires_at),
                principal.expires_at,
            )
            return identifier

    async def get(self, identifier: str, principal: McpSessionPrincipal, *, ttl: int) -> McpSessionMetadata | None:
        from dataclasses import replace

        async with self._lock:
            row = self._rows.get(identifier)
            if not self._valid(row, principal):
                return None
            assert row is not None
            row = replace(
                row,
                expires_at=min(self._clock() + timedelta(seconds=ttl), row.credential_expires_at, principal.expires_at),
            )
            self._rows[identifier] = row
            return deepcopy(row)

    async def initialize(self, identifier: str, principal: McpSessionPrincipal) -> bool:
        from dataclasses import replace

        async with self._lock:
            row = self._rows.get(identifier)
            if not self._valid(row, principal):
                return False
            assert row is not None
            self._rows[identifier] = replace(row, initialized=True)
            return True

    async def delete(self, identifier: str, principal: McpSessionPrincipal) -> bool:
        async with self._lock:
            row = self._rows.get(identifier)
            if not self._valid(row, principal):
                return False
            del self._rows[identifier]
            return True
