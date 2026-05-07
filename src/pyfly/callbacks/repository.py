# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Repository ports + in-memory adapters for the callbacks module."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from pyfly.callbacks.models import CallbackConfig, CallbackExecution, CallbackStatus


@runtime_checkable
class CallbackConfigRepository(Protocol):
    async def save(self, config: CallbackConfig) -> None: ...
    async def get(self, config_id: str) -> CallbackConfig | None: ...
    async def list_by_tenant(self, tenant_id: str) -> list[CallbackConfig]: ...
    async def delete(self, config_id: str) -> bool: ...


@runtime_checkable
class CallbackExecutionRepository(Protocol):
    async def save(self, execution: CallbackExecution) -> None: ...
    async def get(self, execution_id: str) -> CallbackExecution | None: ...
    async def list_pending(self, limit: int = 100) -> list[CallbackExecution]: ...
    async def list_by_config(self, config_id: str) -> list[CallbackExecution]: ...


class InMemoryCallbackConfigRepository:
    def __init__(self) -> None:
        self._store: dict[str, CallbackConfig] = {}
        self._lock = asyncio.Lock()

    async def save(self, config: CallbackConfig) -> None:
        async with self._lock:
            self._store[config.id] = config

    async def get(self, config_id: str) -> CallbackConfig | None:
        async with self._lock:
            return self._store.get(config_id)

    async def list_by_tenant(self, tenant_id: str) -> list[CallbackConfig]:
        async with self._lock:
            return [c for c in self._store.values() if c.tenant_id == tenant_id]

    async def delete(self, config_id: str) -> bool:
        async with self._lock:
            return self._store.pop(config_id, None) is not None


class InMemoryCallbackExecutionRepository:
    def __init__(self) -> None:
        self._store: dict[str, CallbackExecution] = {}
        self._lock = asyncio.Lock()

    async def save(self, execution: CallbackExecution) -> None:
        async with self._lock:
            self._store[execution.id] = execution

    async def get(self, execution_id: str) -> CallbackExecution | None:
        async with self._lock:
            return self._store.get(execution_id)

    async def list_pending(self, limit: int = 100) -> list[CallbackExecution]:
        async with self._lock:
            pending = [e for e in self._store.values() if e.status == CallbackStatus.PENDING]
        return sorted(pending, key=lambda e: e.created_at)[:limit]

    async def list_by_config(self, config_id: str) -> list[CallbackExecution]:
        async with self._lock:
            return [e for e in self._store.values() if e.config_id == config_id]
