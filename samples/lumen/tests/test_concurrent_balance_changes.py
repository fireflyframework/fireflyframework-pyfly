# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Concurrent withdrawals and transfers keep the ``balance >= 0`` invariant.

The :class:`Wallet` aggregate refuses an overdraft of the balance it was loaded
with, and that alone cannot keep the invariant when two requests race: both load
100, both see enough funds for 60, both save. These tests boot the real
application context and send two debits of 60 against a wallet holding 100 at
the same time, through the command bus (``WithdrawFunds``) and through the
money-transfer saga. Exactly one of the two may succeed, and the balance left
must be 40.

The withdrawal handler reads the row with ``LockMode.PESSIMISTIC_WRITE``; the
saga's steps run outside a transaction of their own and debit with one guarded
``UPDATE`` (``WalletRepository.debit``).

Every test runs on a SQLite file database, and on PostgreSQL when
``LUMEN_TEST_POSTGRES_URL`` names a server the test may create databases on,
for example::

    LUMEN_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres \\
        uv run --extra dev --with asyncpg pytest tests/test_concurrent_balance_changes.py

Each PostgreSQL test runs in a database of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

_POSTGRES_URL = os.environ.get("LUMEN_TEST_POSTGRES_URL", "")

_BACKENDS = [
    pytest.param("sqlite-file", id="sqlite-file"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=pytest.mark.skipif(not _POSTGRES_URL, reason="LUMEN_TEST_POSTGRES_URL is not set"),
    ),
]

_ROUNDS = 5


async def _create_postgres_database() -> tuple[str, str]:
    """A new database on the ``LUMEN_TEST_POSTGRES_URL`` server: (its URL, its name)."""
    name = f"lumen_{uuid.uuid4().hex[:12]}"
    server = create_async_engine(_POSTGRES_URL, isolation_level="AUTOCOMMIT")
    try:
        async with server.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await server.dispose()
    return make_url(_POSTGRES_URL).set(database=name).render_as_string(hide_password=False), name


async def _drop_postgres_database(name: str) -> None:
    server = create_async_engine(_POSTGRES_URL, isolation_level="AUTOCOMMIT")
    try:
        async with server.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await server.dispose()


@pytest_asyncio.fixture(params=_BACKENDS)
async def context(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """The booted LumenApplication on a SQLite file database or a PostgreSQL database of its own."""
    database: str | None = None
    if request.param == "postgresql":
        url, database = await _create_postgres_database()
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'lumen-concurrency.db'}"
    monkeypatch.setenv("PYFLY_DATA_RELATIONAL_URL", url)
    logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool").setLevel(logging.CRITICAL)

    from lumen.app import LumenApplication

    from pyfly.core import PyFlyApplication

    app = PyFlyApplication(LumenApplication, config_path=str(_HERE.parent / "pyfly.yaml"))
    await app.startup()
    try:
        yield app.context
    finally:
        await app.shutdown()
        if database is not None:
            await _drop_postgres_database(database)


async def _funded_wallet(commands: Any, owner: str, minor: int) -> str:
    from lumen.core.services.wallets.deposit_funds_command import DepositFunds
    from lumen.core.services.wallets.open_wallet_command import OpenWallet
    from lumen.interfaces.enums.v1.currency import Currency

    wallet_id: str = await commands.send(OpenWallet(owner_id=owner, currency=Currency.EUR))
    if minor:
        await commands.send(DepositFunds(wallet_id=wallet_id, amount=minor))
    return wallet_id


async def _balance(queries: Any, wallet_id: str) -> int:
    from lumen.core.services.wallets.get_wallet_query import GetWallet

    wallet = await queries.query(GetWallet(wallet_id=wallet_id))
    assert wallet is not None
    return int(wallet.balance_minor)


def _rule_of(error: BaseException) -> str | None:
    """The business rule an error was raised for, following its cause chain."""
    from pyfly.domain import BusinessRuleViolation

    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, BusinessRuleViolation):
            return seen.rule
        seen = getattr(seen, "cause", None) or seen.__cause__
    return None


@pytest.mark.asyncio
async def test_two_concurrent_withdrawals_cannot_both_spend_the_same_funds(context: Any) -> None:
    from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds

    from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus

    commands = context.get_bean(DefaultCommandBus)
    queries = context.get_bean(DefaultQueryBus)
    for round_ in range(_ROUNDS):
        wallet_id = await _funded_wallet(commands, f"owner-{round_}", 100)

        outcomes = await asyncio.gather(
            commands.send(WithdrawFunds(wallet_id=wallet_id, amount=60)),
            commands.send(WithdrawFunds(wallet_id=wallet_id, amount=60)),
            return_exceptions=True,
        )

        succeeded = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert succeeded == [40], outcomes
        assert [_rule_of(error) for error in refused] == ["wallet-insufficient-funds"], outcomes
        assert await _balance(queries, wallet_id) == 40


@pytest.mark.asyncio
async def test_two_concurrent_transfers_cannot_both_spend_the_same_funds(context: Any) -> None:
    from lumen.core.services.transfers import TransferRequest, TransferService
    from lumen.interfaces.enums.v1.currency import Currency

    from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus

    commands = context.get_bean(DefaultCommandBus)
    queries = context.get_bean(DefaultQueryBus)
    transfers = context.get_bean(TransferService)
    for round_ in range(_ROUNDS):
        source = await _funded_wallet(commands, f"source-{round_}", 100)
        first = await _funded_wallet(commands, f"first-{round_}", 0)
        second = await _funded_wallet(commands, f"second-{round_}", 0)

        outcomes = await asyncio.gather(
            transfers.transfer(TransferRequest(source, first, 60, Currency.EUR)),
            transfers.transfer(TransferRequest(source, second, 60, Currency.EUR)),
        )

        assert sorted(outcome["status"] for outcome in outcomes) == ["completed", "failed"], outcomes
        assert await _balance(queries, source) == 40
        assert await _balance(queries, first) + await _balance(queries, second) == 60
