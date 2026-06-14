# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Integration test — boot the REAL application context end to end.

This is the composition no single framework test covers: it boots the
actual :class:`LumenApplication` (DI scan, CQRS auto-config, relational
auto-config, EDA, the ``@transactional`` seam, the
``RepositoryBeanPostProcessor`` that compiles the derived/spec queries),
then drives the whole wallet lifecycle through the **real** command and
query buses resolved from the context:

    open -> deposit -> withdraw -> list (paged) -> rich (Specification) -> balance

and proves persistence survives by reloading via ``GetWallet``. The
relational URL is pointed at an isolated temp-file SQLite database via the
``PYFLY_DATA_RELATIONAL_URL`` env override so the test never touches the
developer's ``lumen.db``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))


@pytest_asyncio.fixture
async def booted_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[object]:
    """Boot the full LumenApplication against an isolated SQLite file."""
    db_path = tmp_path / "lumen-it.db"
    monkeypatch.setenv("PYFLY_DATA_RELATIONAL_URL", f"sqlite+aiosqlite:///{db_path}")
    # aiosqlite + async-engine dispose can leave a pooled connection to be
    # reaped by the GC at interpreter teardown, which SQLAlchemy logs as an
    # error. It is harmless (the test passes and persistence is verified by
    # reload); quiet just that pool logger so the run output stays clean.
    logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool").setLevel(logging.CRITICAL)

    from lumen.app import LumenApplication

    from pyfly.core import PyFlyApplication

    app = PyFlyApplication(LumenApplication, config_path=str(_HERE.parent / "pyfly.yaml"))
    await app.startup()
    try:
        yield app.context
    finally:
        # Return the framework's shared, pooled AsyncSession to the pool
        # before the engine is disposed, so no aiosqlite connection lingers
        # to be reaped by the garbage collector at teardown.
        await app.context.get_bean(AsyncSession).close()
        await app.shutdown()


@pytest.mark.asyncio
async def test_full_lifecycle_through_booted_context(booted_context: object) -> None:
    from lumen.core.services.wallets.deposit_funds_command import DepositFunds
    from lumen.core.services.wallets.get_balance_query import GetBalance
    from lumen.core.services.wallets.get_wallet_query import GetWallet
    from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
    from lumen.core.services.wallets.list_wallets_query import ListWallets
    from lumen.core.services.wallets.open_wallet_command import OpenWallet
    from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
    from lumen.interfaces.enums.v1.currency import Currency

    from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
    from pyfly.data import Pageable

    ctx = booted_context
    commands: DefaultCommandBus = ctx.get_bean(DefaultCommandBus)  # type: ignore[attr-defined]
    queries: DefaultQueryBus = ctx.get_bean(DefaultQueryBus)  # type: ignore[attr-defined]

    # --- open -> deposit -> withdraw, each a committed unit of work ------
    w1 = await commands.send(OpenWallet(owner_id="u-1", currency=Currency.EUR))
    w2 = await commands.send(OpenWallet(owner_id="u-2", currency=Currency.EUR))
    assert w1.startswith("wlt-") and w2.startswith("wlt-")

    assert await commands.send(DepositFunds(wallet_id=w1, amount=5000)) == 5000
    assert await commands.send(WithdrawFunds(wallet_id=w1, amount=1500)) == 3500
    assert await commands.send(DepositFunds(wallet_id=w2, amount=100)) == 100

    # --- persistence survived: reload the aggregate via the query side --
    reloaded = await queries.query(GetWallet(wallet_id=w1))
    assert reloaded is not None
    assert reloaded.owner_id == "u-1"
    assert reloaded.balance_minor == 3500

    # --- paged list (find_all(pageable) + Page.map) ---------------------
    page = await queries.query(ListWallets(pageable=Pageable.of(1, 10)))
    assert page.total == 2
    assert {w.id for w in page.items} == {w1, w2}

    # --- Specification: only wallets with balance >= 1000 ---------------
    rich = await queries.query(ListRichWallets(min_minor=1000, pageable=Pageable.of(1, 10)))
    assert rich.total == 1
    assert [w.id for w in rich.items] == [w1]

    # min_minor=0 returns everyone
    everyone = await queries.query(ListRichWallets(min_minor=0, pageable=Pageable.of(1, 10)))
    assert everyone.total == 2

    # --- projection-backed balance --------------------------------------
    balance = await queries.query(GetBalance(wallet_id=w1))
    assert balance is not None
    assert balance.balance_minor == 3500
    assert balance.balance == 35.0
