# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Wallet repository — port + in-memory adapter.

The :class:`WalletRepository` protocol is the hexagonal *port* the core
depends on. :class:`InMemoryWalletRepository` is one *adapter*. Swap in a
SQLAlchemy/SQLite-backed adapter (see ``sql_wallet_repository.py``) and
the business logic stays unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol, runtime_checkable

from lumen.models.entities.v1.wallet_entity import Wallet
from pyfly.container import repository


@runtime_checkable
class WalletRepository(Protocol):
    async def add(self, wallet: Wallet) -> Wallet: ...
    async def find(self, id: str) -> Wallet | None: ...
    async def remove(self, wallet: Wallet) -> None: ...
    async def next_id(self) -> str: ...


@repository
class InMemoryWalletRepository(WalletRepository):
    """Concurrent in-memory store keyed by wallet id.

    Explicitly implements the :class:`WalletRepository` port so the DI
    container auto-binds the port to this adapter — inject the port
    anywhere and you get this implementation.
    """

    def __init__(self) -> None:
        self._store: dict[str, Wallet] = {}
        self._lock = asyncio.Lock()

    async def add(self, wallet: Wallet) -> Wallet:
        async with self._lock:
            assert wallet.id is not None
            self._store[wallet.id] = wallet
            return wallet

    async def find(self, id: str) -> Wallet | None:
        async with self._lock:
            return self._store.get(id)

    async def remove(self, wallet: Wallet) -> None:
        async with self._lock:
            if wallet.id is not None:
                self._store.pop(wallet.id, None)

    async def next_id(self) -> str:
        return f"wlt-{uuid.uuid4()}"
