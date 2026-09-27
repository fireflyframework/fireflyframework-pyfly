# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``@transactional`` on a real SQLite file database (foreign keys on), through a real ApplicationContext.

Nothing here mocks a session or an engine: every outcome is read back from the database with an engine of
its own, and every test ends with no pooled connection checked out.

- WP01-04 (C004): a caught participant failure makes the outermost boundary roll back and raise
  ``UnexpectedRollbackError``; a caught repository DB error does too.
- WP01-06 (C059, C173): rollback rules are additive, the most specific rule wins, and a
  ``no_rollback_for`` exception on a dead transaction re-raises the original exception.
- WP01-09 (C060): ``datasource=`` and repository ``__datasource__`` put each unit on its own database.
- WP01-10 (C063): ``NESTED`` savepoints and ``timeout``.
- WP01-11 (C061): ``reactive_transactional`` binds its unit.
- WP01-14 (C084): the documented service with no factory attribute runs on the default datasource.
- WP01-15 (C143): a polyglot service fails fast.
- WP01-16 (C174): decoration-time validation, class decoration, plain functions.
- WP01-01 (C006): every holder shape (deep, list, dict, Provider, get_bean, injected AsyncSession,
  argument-passed) writes inside the one unit.
- WP01-05 (C005): ``gather`` inside a unit is serialized; a task that outlives its unit fails loudly;
  ``detached`` work gets its own transactions.
- WP01-08 (F7): read-only is enforced, routed to the replica, and visible to ``is_read_only()``.
- Synchronizations: before-commit, after-commit (state cleared, failures counted), after-completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.provider import Provider
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data import Isolation, Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.routing import is_read_only
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transactional import reactive_transactional
from pyfly.data.transaction import (
    CompletionStatus,
    IllegalTransactionStateError,
    TransactionSynchronizationAdapter,
    TransactionTimedOutError,
    UnexpectedRollbackError,
    after_commit,
    current_unit_of_work,
    detached,
    is_transaction_active,
    register_synchronization,
    rollback_on,
)

# ---------------------------------------------------------------------------------------------------------
# Model, repositories and services
# ---------------------------------------------------------------------------------------------------------


class TxItem(Base):
    __tablename__ = "tx_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class TxAudit(Base):
    __tablename__ = "tx_audit"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class TxReport(Base):
    __tablename__ = "tx_report"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class TxItemRepository(Repository[TxItem, int]):
    pass


@repository
class TxAuditRepository(Repository[TxAudit, int]):
    pass


@repository
class TxReportRepository(Repository[TxReport, int]):
    __datasource__ = "reporting"


class PaymentError(Exception):
    pass


class InsufficientFunds(PaymentError):
    pass


@service
class Inventory:
    """A participant service: no factory attribute, so it runs on the default datasource (C084)."""

    def __init__(self, items: TxItemRepository, audits: TxAuditRepository) -> None:
        self.items = items
        self.audits = audits

    @transactional
    async def reserve(self, name: str, *, fail: bool = False) -> None:
        await self.items.save(TxItem(name=name))
        if fail:
            raise ValueError(f"{name} failed")

    @transactional(propagation=Propagation.MANDATORY)
    async def reserve_mandatory(self, name: str, *, fail: bool = False) -> None:
        await self.items.save(TxItem(name=name))
        if fail:
            raise ValueError(f"{name} failed")

    @transactional(propagation=Propagation.NESTED)
    async def try_step(self, name: str, *, fail: bool = False) -> None:
        await self.items.save(TxItem(name=name))
        if fail:
            raise ValueError(f"{name} failed")

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def audit_new(self, name: str) -> None:
        await self.audits.save(TxAudit(name=name))

    @transactional(propagation=Propagation.NEVER)
    async def never(self) -> str:
        return "ran"

    @transactional(propagation=Propagation.SUPPORTS)
    async def supports(self) -> bool:
        return is_transaction_active()

    @transactional(propagation=Propagation.NOT_SUPPORTED)
    async def not_supported(self) -> bool:
        return is_transaction_active()


@service
class Orders:
    def __init__(self, items: TxItemRepository, inventory: Inventory) -> None:
        self.items = items
        self.inventory = inventory

    @transactional
    async def place_catching_participant(self) -> str:
        await self.items.save(TxItem(name="outer-1"))
        with contextlib.suppress(ValueError):
            await self.inventory.reserve("inner-partial", fail=True)
        await self.items.save(TxItem(name="outer-2"))
        return "returned normally"

    @transactional
    async def place_catching_mandatory(self) -> None:
        await self.items.save(TxItem(name="outer-1"))
        with contextlib.suppress(ValueError):
            await self.inventory.reserve_mandatory("inner-mandatory-partial", fail=True)

    @transactional
    async def place_catching_db_error(self) -> None:
        await self.items.save(TxItem(name="dup"))
        with contextlib.suppress(IntegrityError):
            await self.items.save(TxItem(name="dup"))

    @transactional
    async def place_with_nested_step(self, *, fail: bool) -> None:
        await self.items.save(TxItem(name="before"))
        with contextlib.suppress(ValueError):
            await self.inventory.try_step("nested", fail=fail)
        await self.items.save(TxItem(name="after"))

    @transactional
    async def place_with_nested_db_error(self) -> None:
        await self.items.save(TxItem(name="taken"))
        with contextlib.suppress(IntegrityError):
            await self.inventory.try_step("taken")
        await self.items.save(TxItem(name="after"))

    @transactional(rollback_for=(PaymentError,))
    async def narrowed_then_bug(self) -> None:
        await self.items.save(TxItem(name="half-written-by-buggy-method"))
        raise KeyError("missing")

    @transactional(no_rollback_for=(KeyError,))
    async def tolerated(self) -> None:
        await self.items.save(TxItem(name="kept"))
        raise KeyError("tolerated")

    @transactional(rollback_for=(InsufficientFunds,), no_rollback_for=(PaymentError,))
    async def specific_rule(self, error: Exception) -> None:
        await self.items.save(TxItem(name="specific"))
        raise error

    @transactional(no_rollback_for=(IntegrityError,))
    async def tolerated_db_error(self) -> None:
        await self.items.save(TxItem(name="good"))
        await self.items.save(TxItem(name="seed"))

    @transactional
    async def cancelled(self) -> None:
        await self.items.save(TxItem(name="never"))
        raise asyncio.CancelledError

    @transactional(timeout=0.2)
    async def slow(self) -> None:
        await self.items.save(TxItem(name="slow"))
        await asyncio.sleep(5)

    @transactional(isolation=Isolation.READ_COMMITTED)
    async def read_committed(self) -> int:
        return await self.items.count()

    @transactional(isolation=Isolation.SERIALIZABLE)
    async def serializable(self) -> int:
        return await self.items.count()

    @transactional(read_only=True)
    async def read_only_view(self) -> tuple[bool, bool, int]:
        session = self.items._session
        return is_read_only(), bool(session.info.get("read_only")), await self.items.count()

    @transactional(read_only=True)
    async def read_only_write(self) -> None:
        await self.items.save(TxItem(name="written-inside-read-only"))

    @transactional(read_only=True)
    async def read_then_audit_new(self) -> None:
        await self.items.count()
        await self.inventory.audit_new("audit-row")
        raise ValueError("the outer read fails afterwards")

    @transactional
    async def write_then_audit_new(self) -> None:
        await self.items.save(TxItem(name="outer"))
        await self.inventory.audit_new("audit-row")

    @transactional
    async def with_never(self) -> str:
        return await self.inventory.never()

    @transactional
    async def with_supports(self) -> bool:
        return await self.inventory.supports()

    @transactional
    async def with_not_supported(self) -> bool:
        return await self.inventory.not_supported()


@service
class LegacyOrders:
    """The pre-registry shape: the session factory on ``self._session_factory``."""

    def __init__(self, items: TxItemRepository, factory: async_sessionmaker[AsyncSession]) -> None:
        self.items = items
        self._session_factory = factory

    @transactional
    async def place(self, name: str, *, fail: bool = False) -> None:
        await self.items.save(TxItem(name=name))
        if fail:
            raise ValueError("rolled back")


@transactional(datasource="reporting")
class ReportingService:
    """Class-level ``@transactional`` naming a datasource; a method's own settings win."""

    def __init__(self, reports: TxReportRepository) -> None:
        self.reports = reports

    async def record(self, name: str, *, fail: bool = False) -> None:
        await self.reports.save(TxReport(name=name))
        if fail:
            raise ValueError("report failed")

    @transactional(propagation=Propagation.MANDATORY)
    async def record_mandatory(self, name: str) -> None:
        await self.reports.save(TxReport(name=name))

    async def _private_helper(self) -> bool:
        return is_transaction_active("reporting")

    def sync_helper(self) -> str:
        return "untouched"


@service
class MixedDatasources:
    def __init__(self, items: TxItemRepository, reporting: ReportingService) -> None:
        self.items = items
        self.reporting = reporting

    @transactional
    async def place_and_report(self, *, fail: bool) -> None:
        await self.items.save(TxItem(name="order"))
        await self.reporting.record("report-for-order")
        if fail:
            raise ValueError("the order fails after the report committed on its own datasource")

    @transactional
    async def place_and_report_mandatory(self) -> None:
        await self.items.save(TxItem(name="order"))
        await self.reporting.record_mandatory("mandatory-report")


# ---------------------------------------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------------------------------------

_BASE_BEANS: tuple[type, ...] = (
    RelationalAutoConfiguration,
    TxItemRepository,
    TxAuditRepository,
    TxReportRepository,
    Inventory,
    Orders,
    LegacyOrders,
    ReportingService,
    MixedDatasources,
)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _create_tables(url: str, *models: Any) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[model.__table__ for model in models])
    finally:
        await engine.dispose()


async def _rows(url: str, table: str) -> list[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in (await conn.execute(text(f"SELECT name FROM {table} ORDER BY id"))).all()]
    finally:
        await engine.dispose()


class App:
    """A started ApplicationContext on a SQLite file (plus a 'reporting' file), with its URLs."""

    def __init__(self, ctx: ApplicationContext, url: str, reporting_url: str) -> None:
        self.ctx = ctx
        self.url = url
        self.reporting_url = reporting_url

    def bean(self, cls: type[Any]) -> Any:
        return self.ctx.get_bean(cls)

    async def items(self) -> list[str]:
        return await _rows(self.url, "tx_item")

    async def audits(self) -> list[str]:
        return await _rows(self.url, "tx_audit")

    async def reports(self) -> list[str]:
        return await _rows(self.reporting_url, "tx_report")

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())


async def _start(tmp_path: Path, overrides: dict[str, Any] | None = None, beans: tuple[type, ...] = ()) -> App:
    url = _url(tmp_path / "app.db")
    reporting_url = _url(tmp_path / "reporting.db")
    await _create_tables(url, TxItem, TxAudit)
    await _create_tables(reporting_url, TxReport)
    relational: dict[str, Any] = {
        "enabled": "true",
        "url": url,
        "ddl-auto": "none",
        "datasources": {"reporting": {"url": reporting_url}},
    }
    relational.update(overrides or {})
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": relational}}}))
    for bean in (*_BASE_BEANS, *beans):
        ctx.register_bean(bean)
    await ctx.start()
    return App(ctx, url, reporting_url)


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    started = await _start(tmp_path)
    try:
        yield started
        assert started.checked_out() == 0, "a test left a pooled connection checked out"
    finally:
        await started.ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Enums and metadata
# ---------------------------------------------------------------------------------------------------------


class TestEnumsAndMetadata:
    def test_seven_propagations_including_nested(self) -> None:
        assert [p.value for p in Propagation] == [
            "REQUIRED",
            "REQUIRES_NEW",
            "NESTED",
            "SUPPORTS",
            "NOT_SUPPORTED",
            "NEVER",
            "MANDATORY",
        ]

    def test_isolation_values(self) -> None:
        assert Isolation.READ_COMMITTED.value == "READ COMMITTED"
        assert len(Isolation) == 5

    def test_metadata_is_kept(self) -> None:
        assert Orders.place_catching_participant.__pyfly_transactional__ is True  # type: ignore[attr-defined]
        assert Inventory.try_step.__pyfly_propagation__ is Propagation.NESTED  # type: ignore[attr-defined]
        assert Orders.serializable.__pyfly_isolation__ is Isolation.SERIALIZABLE  # type: ignore[attr-defined]

    def test_the_legacy_import_paths_are_the_same_objects(self) -> None:
        from pyfly.data.document.mongodb import mongo_transactional
        from pyfly.data.relational.sqlalchemy import transactional as relational_transactional
        from pyfly.data.transaction import Propagation as NeutralPropagation
        from pyfly.data.transaction import transactional as neutral_transactional

        assert transactional is relational_transactional is neutral_transactional
        assert mongo_transactional is transactional
        assert Propagation is NeutralPropagation


# ---------------------------------------------------------------------------------------------------------
# Rollback rules (C059, C173)
# ---------------------------------------------------------------------------------------------------------


class TestRollbackRules:
    def test_default_rolls_back_every_exception(self) -> None:
        assert rollback_on(KeyError("x"))
        assert rollback_on(asyncio.CancelledError())

    def test_rollback_for_is_additive(self) -> None:
        assert rollback_on(KeyError("x"), rollback_for=(PaymentError,))

    def test_no_rollback_for_commits(self) -> None:
        assert not rollback_on(KeyError("x"), no_rollback_for=(KeyError,))

    def test_the_most_specific_rule_wins(self) -> None:
        rules = {"rollback_for": (InsufficientFunds,), "no_rollback_for": (PaymentError,)}
        assert rollback_on(InsufficientFunds(), **rules)
        assert not rollback_on(PaymentError(), **rules)

    def test_a_tie_rolls_back(self) -> None:
        assert rollback_on(PaymentError(), rollback_for=(PaymentError,), no_rollback_for=(PaymentError,))

    def test_a_base_exception_always_rolls_back(self) -> None:
        assert rollback_on(asyncio.CancelledError(), no_rollback_for=(BaseException,))

    async def test_narrowed_rollback_for_still_rolls_back_a_bug(self, app: App) -> None:
        with pytest.raises(KeyError):
            await app.bean(Orders).narrowed_then_bug()
        assert await app.items() == []

    async def test_no_rollback_for_commits_and_reraises(self, app: App) -> None:
        with pytest.raises(KeyError):
            await app.bean(Orders).tolerated()
        assert await app.items() == ["kept"]

    async def test_most_specific_rule_on_a_real_unit(self, app: App) -> None:
        with pytest.raises(InsufficientFunds):
            await app.bean(Orders).specific_rule(InsufficientFunds())
        assert await app.items() == []
        with pytest.raises(PaymentError):
            await app.bean(Orders).specific_rule(PaymentError())
        assert await app.items() == ["specific"]

    async def test_no_rollback_for_on_a_dead_transaction_reraises_the_original(self, app: App) -> None:
        await app.bean(LegacyOrders).place("seed")
        with pytest.raises(IntegrityError):
            await app.bean(Orders).tolerated_db_error()
        assert await app.items() == ["seed"]

    async def test_cancellation_rolls_back(self, app: App) -> None:
        with pytest.raises(asyncio.CancelledError):
            await app.bean(Orders).cancelled()
        assert await app.items() == []


# ---------------------------------------------------------------------------------------------------------
# Rollback-only (C004)
# ---------------------------------------------------------------------------------------------------------


class TestRollbackOnly:
    async def test_a_caught_required_participant_failure_rolls_the_unit_back(self, app: App) -> None:
        with pytest.raises(UnexpectedRollbackError, match="rollback-only") as raised:
            await app.bean(Orders).place_catching_participant()
        assert isinstance(raised.value.__cause__, ValueError)
        assert await app.items() == []

    async def test_a_caught_mandatory_participant_failure_rolls_back_too(self, app: App) -> None:
        with pytest.raises(UnexpectedRollbackError):
            await app.bean(Orders).place_catching_mandatory()
        assert await app.items() == []

    async def test_a_caught_repository_db_error_rolls_back(self, app: App) -> None:
        with pytest.raises(UnexpectedRollbackError):
            await app.bean(Orders).place_catching_db_error()
        assert await app.items() == []


# ---------------------------------------------------------------------------------------------------------
# Propagation (C007, C063)
# ---------------------------------------------------------------------------------------------------------


class TestPropagation:
    async def test_nested_failure_rolls_back_to_the_savepoint_only(self, app: App) -> None:
        await app.bean(Orders).place_with_nested_step(fail=True)
        assert await app.items() == ["before", "after"]

    async def test_nested_success_is_kept(self, app: App) -> None:
        await app.bean(Orders).place_with_nested_step(fail=False)
        assert await app.items() == ["before", "nested", "after"]

    async def test_a_db_error_inside_nested_does_not_doom_the_outer_unit(self, app: App) -> None:
        await app.bean(Orders).place_with_nested_db_error()
        assert await app.items() == ["taken", "after"]

    async def test_nested_without_a_unit_begins_one(self, app: App) -> None:
        await app.bean(Inventory).try_step("alone")
        assert await app.items() == ["alone"]

    async def test_requires_new_commits_and_resumes_the_outer_unit(self, app: App) -> None:
        # The outer unit only reads (SQLite has one writer), then fails after the inner unit committed.
        with pytest.raises(ValueError):
            await app.bean(Orders).read_then_audit_new()
        assert await app.audits() == ["audit-row"]
        assert not is_transaction_active()

    async def test_requires_new_inside_a_sqlite_write_unit_fails_fast(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="one writer"):
            await app.bean(Orders).write_then_audit_new()
        assert await app.items() == []
        assert await app.audits() == []

    async def test_mandatory_without_a_unit_raises(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="MANDATORY"):
            await app.bean(Inventory).reserve_mandatory("x")

    async def test_never_inside_a_unit_raises(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="NEVER"):
            await app.bean(Orders).with_never()
        assert await app.bean(Inventory).never() == "ran"

    async def test_supports_joins_or_runs_without(self, app: App) -> None:
        assert await app.bean(Orders).with_supports() is True
        assert await app.bean(Inventory).supports() is False

    async def test_not_supported_suspends(self, app: App) -> None:
        assert await app.bean(Orders).with_not_supported() is False


# ---------------------------------------------------------------------------------------------------------
# Isolation, read-only, timeout (F6, F7, C063)
# ---------------------------------------------------------------------------------------------------------


class TestIsolationReadOnlyTimeout:
    async def test_serializable_runs_on_sqlite(self, app: App) -> None:
        assert await app.bean(Orders).serializable() == 0

    async def test_an_unsupported_level_fails_at_begin(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="READ COMMITTED"):
            await app.bean(Orders).read_committed()

    async def test_read_only_is_visible_and_flagged(self, app: App) -> None:
        assert await app.bean(Orders).read_only_view() == (True, True, 0)
        assert is_read_only() is False

    async def test_a_write_inside_read_only_is_refused(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="read-only"):
            await app.bean(Orders).read_only_write()
        assert await app.items() == []

    async def test_timeout_rolls_back_and_raises(self, app: App) -> None:
        with pytest.raises(TransactionTimedOutError) as raised:
            await app.bean(Orders).slow()
        assert isinstance(raised.value, TimeoutError)
        assert await app.items() == []

    async def test_read_only_routes_to_the_replica(self, tmp_path: Path) -> None:
        replica_url = _url(tmp_path / "replica.db")
        await _create_tables(replica_url, TxItem)
        engine = create_async_engine(replica_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO tx_item (name) VALUES ('on-the-replica')"))
        await engine.dispose()
        started = await _start(tmp_path, {"read-replica": {"url": replica_url}})
        try:
            orders = started.bean(Orders)
            assert (await orders.read_only_view())[2] == 1  # the replica's row
            assert await orders.items.count() == 0  # the primary, outside a read-only unit
        finally:
            await started.ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Resolution: datasource qualifier, class decoration, legacy attributes, plain functions (C060, C084, C143)
# ---------------------------------------------------------------------------------------------------------


class TestResolution:
    async def test_a_service_without_a_factory_runs_on_the_default_datasource(self, app: App) -> None:
        await app.bean(Inventory).reserve("readme-style")
        assert await app.items() == ["readme-style"]

    async def test_the_legacy_session_factory_attribute_still_works(self, app: App) -> None:
        await app.bean(LegacyOrders).place("legacy")
        with pytest.raises(ValueError):
            await app.bean(LegacyOrders).place("legacy-rolled-back", fail=True)
        assert await app.items() == ["legacy"]

    async def test_class_level_datasource_puts_the_unit_on_that_database(self, app: App) -> None:
        await app.bean(ReportingService).record("r1")
        assert await app.reports() == ["r1"]
        assert await app.items() == []

    async def test_required_on_another_datasource_begins_its_own_unit(self, app: App) -> None:
        with pytest.raises(ValueError):
            await app.bean(MixedDatasources).place_and_report(fail=True)
        assert await app.items() == []
        assert await app.reports() == ["report-for-order"]

    async def test_mandatory_does_not_join_another_datasource(self, app: App) -> None:
        with pytest.raises(IllegalTransactionStateError, match="MANDATORY"):
            await app.bean(MixedDatasources).place_and_report_mandatory()
        assert await app.items() == []

    async def test_class_decoration_keeps_the_class_and_skips_non_public_and_sync(self) -> None:
        assert inspect.isclass(ReportingService)
        assert getattr(ReportingService.record, "__pyfly_transactional__", False)
        assert not getattr(ReportingService._private_helper, "__pyfly_transactional__", False)
        assert ReportingService.sync_helper(ReportingService.__new__(ReportingService)) == "untouched"
        definition = ReportingService.record_mandatory.__pyfly_transaction_definition__  # type: ignore[attr-defined]
        assert definition.propagation is Propagation.MANDATORY
        assert definition.datasource == "reporting"

    async def test_the_deprecated_relational_runner_still_runs_a_unit(self, app: App) -> None:
        from pyfly.data.relational.sqlalchemy.transactional import run_relational_transaction

        legacy = app.bean(LegacyOrders)

        async def place(service: LegacyOrders, name: str, *, fail: bool = False) -> None:
            await service.items.save(TxItem(name=name))
            if fail:
                raise ValueError("rolled back")

        with pytest.warns(DeprecationWarning, match="run_relational_transaction"):
            await run_relational_transaction(place, (legacy, "via-runner"), {})
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError):
            await run_relational_transaction(place, (legacy, "runner-rolled-back"), {"fail": True})
        assert await app.items() == ["via-runner"]

    async def test_a_manager_and_a_datasource_that_disagree_are_refused(self, app: App) -> None:
        from pyfly.data.transaction import resolve_manager

        @transactional(manager=resolve_manager("primary"), datasource="reporting")
        async def ambiguous() -> None:
            pytest.fail("the body must not run")

        with pytest.raises(IllegalTransactionStateError, match="reporting"):
            await ambiguous()

    async def test_a_plain_function_runs_on_the_default_datasource(self, app: App) -> None:
        items = app.bean(TxItemRepository)

        @transactional
        async def standalone(name: str) -> bool:
            await items.save(TxItem(name=name))
            return is_transaction_active()

        assert await standalone("from-a-function") is True
        assert await app.items() == ["from-a-function"]

    async def test_a_polyglot_service_fails_fast(self, app: App) -> None:
        from pymongo import AsyncMongoClient

        client: AsyncMongoClient[Any] = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)

        class Polyglot:
            def __init__(self) -> None:
                self._session_factory = app.bean(async_sessionmaker)
                self._motor_client = client

            @transactional
            async def write_both(self) -> None:
                raise AssertionError("must not run")

            @transactional(datasource="primary")
            async def write_relational(self) -> bool:
                return is_transaction_active("primary")

        try:
            with pytest.raises(IllegalTransactionStateError, match="both"):
                await Polyglot().write_both()
            assert await Polyglot().write_relational() is True
        finally:
            await client.close()

    def test_sync_functions_and_async_generators_are_rejected_at_decoration(self) -> None:
        with pytest.raises(TypeError, match="async def"):

            @transactional
            def sync_method(self: object) -> None: ...

        with pytest.raises(TypeError, match="async generator"):

            @transactional(read_only=True)
            async def stream(self: object) -> AsyncIterator[int]:
                yield 1

    async def test_no_manager_outside_a_context_is_a_clear_error(self) -> None:
        @transactional
        async def orphan() -> None: ...

        with pytest.raises(IllegalTransactionStateError, match="no application context"):
            await orphan()


# ---------------------------------------------------------------------------------------------------------
# reactive_transactional (C061)
# ---------------------------------------------------------------------------------------------------------


class TestReactiveTransactional:
    async def test_it_binds_its_unit_for_nested_transactional_code(self, app: App) -> None:
        factory = app.bean(async_sessionmaker)
        inventory = app.bean(Inventory)

        @reactive_transactional(factory)
        async def batch(session: AsyncSession, *, fail: bool) -> None:
            session.add(TxItem(name="outer-row"))
            await session.flush()
            await inventory.reserve("inner-row")
            await inventory.reserve_mandatory("mandatory-row")
            if fail:
                raise ValueError("the batch fails")

        with pytest.raises(ValueError):
            await batch(fail=True)
        assert await app.items() == []
        await batch(fail=False)
        assert await app.items() == ["outer-row", "inner-row", "mandatory-row"]

    def test_it_rejects_a_sync_function(self) -> None:
        with pytest.raises(TypeError):
            reactive_transactional(async_sessionmaker())(lambda session: None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------------
# Holder shapes (C006) and child tasks (C005)
# ---------------------------------------------------------------------------------------------------------


@service
class DeepHolder:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory


@service
class ShapeWriter:
    """Writes through every shape the old structural patching could not reach."""

    def __init__(
        self,
        items: TxItemRepository,
        deep: DeepHolder,
        repos: list[TxAuditRepository],
        provider: Provider[TxAuditRepository],
        session: AsyncSession,
    ) -> None:
        self.items = items
        self.nested = {"level": [deep]}  # two levels plus a container
        self.repos = repos
        self.by_name = {"audit": repos[0]}  # a strategy registry keyed by name
        self.provider = provider
        self.session = session
        self.context: ApplicationContext | None = None

    @transactional
    async def write_everywhere(self, helper: Inventory, *, fail: bool) -> None:
        await self.items.save(TxItem(name="direct"))
        await self.nested["level"][0].inventory.reserve("deep")
        await self.repos[0].save(TxAudit(name="list"))
        await next(iter(self.by_name.values())).save(TxAudit(name="dict"))
        await self.provider.get().save(TxAudit(name="provider"))
        assert self.context is not None
        await self.context.get_bean(TxAuditRepository).save(TxAudit(name="get_bean"))
        self.session.add(TxAudit(name="session"))
        await self.session.flush()
        await helper.reserve("argument")
        if fail:
            raise ValueError("everything rolls back together")

    @transactional
    async def fan_out(self, inventory: Inventory) -> None:
        await asyncio.gather(*(inventory.reserve(f"child-{i}") for i in range(5)))

    @transactional
    async def leave_a_task_behind(self, inventory: Inventory, started: asyncio.Event) -> asyncio.Task[None]:
        await self.items.save(TxItem(name="root"))

        async def late() -> None:
            await started.wait()
            await inventory.reserve("late")

        return asyncio.get_running_loop().create_task(late())

    @transactional
    async def leave_a_requires_new_task_behind(
        self, inventory: Inventory, started: asyncio.Event
    ) -> asyncio.Task[None]:
        await self.items.save(TxItem(name="root"))

        async def late() -> None:
            await started.wait()
            await inventory.audit_new("late-audit")  # REQUIRES_NEW does not use the completed unit

        return asyncio.get_running_loop().create_task(late())

    @transactional
    async def detach_work(self, inventory: Inventory) -> asyncio.Task[None]:
        await self.items.save(TxItem(name="root"))
        return detached(inventory.reserve("detached"))


class TestHolderShapesAndTasks:
    async def _app(self, tmp_path: Path) -> App:
        started = await _start(tmp_path, beans=(DeepHolder, ShapeWriter))
        started.bean(ShapeWriter).context = started.ctx
        return started

    async def test_every_holder_shape_writes_inside_the_unit(self, tmp_path: Path) -> None:
        started = await self._app(tmp_path)
        try:
            writer = started.bean(ShapeWriter)
            with pytest.raises(ValueError):
                await writer.write_everywhere(started.bean(Inventory), fail=True)
            assert await started.items() == []
            assert await started.audits() == []
            await writer.write_everywhere(started.bean(Inventory), fail=False)
            assert await started.items() == ["direct", "deep", "argument"]
            assert await started.audits() == ["list", "dict", "provider", "get_bean", "session"]
            assert started.checked_out() == 0
        finally:
            await started.ctx.stop()

    async def test_gather_inside_a_unit_is_serialized_and_atomic(self, tmp_path: Path) -> None:
        started = await self._app(tmp_path)
        try:
            await started.bean(ShapeWriter).fan_out(started.bean(Inventory))
            assert sorted(await started.items()) == [f"child-{i}" for i in range(5)]
            assert started.checked_out() == 0
        finally:
            await started.ctx.stop()

    async def test_a_task_that_outlives_its_unit_fails_loudly(self, tmp_path: Path) -> None:
        started = await self._app(tmp_path)
        try:
            gate = asyncio.Event()
            task = await started.bean(ShapeWriter).leave_a_task_behind(started.bean(Inventory), gate)
            gate.set()
            with pytest.raises(IllegalTransactionStateError, match="already committed"):
                await task
            assert await started.items() == ["root"]
            assert started.checked_out() == 0
        finally:
            await started.ctx.stop()

    async def test_requires_new_from_a_task_that_outlived_its_unit_works(self, tmp_path: Path) -> None:
        started = await self._app(tmp_path)
        try:
            gate = asyncio.Event()
            task = await started.bean(ShapeWriter).leave_a_requires_new_task_behind(started.bean(Inventory), gate)
            gate.set()
            await task
            assert await started.items() == ["root"]
            assert await started.audits() == ["late-audit"]
            assert started.checked_out() == 0
        finally:
            await started.ctx.stop()

    async def test_detached_work_gets_its_own_transaction(self, tmp_path: Path) -> None:
        started = await self._app(tmp_path)
        try:
            task = await started.bean(ShapeWriter).detach_work(started.bean(Inventory))
            await task
            assert await started.items() == ["root", "detached"]
            assert started.checked_out() == 0
        finally:
            await started.ctx.stop()

    def test_detached_rejects_other_values(self) -> None:
        with pytest.raises(TypeError):
            detached(42)  # type: ignore[call-overload]

    async def test_detached_decorator_schedules_a_task(self) -> None:
        @detached
        async def work() -> bool:
            return is_transaction_active()

        assert await work() is False


# ---------------------------------------------------------------------------------------------------------
# Synchronizations
# ---------------------------------------------------------------------------------------------------------


class Recorder(TransactionSynchronizationAdapter):
    def __init__(self, log: list[str], *, fail_after_commit: bool = False) -> None:
        self.log = log
        self.fail_after_commit = fail_after_commit

    async def before_commit(self, read_only: bool) -> None:
        self.log.append(f"before_commit(read_only={read_only}, active={is_transaction_active()})")

    async def before_completion(self) -> None:
        self.log.append("before_completion")

    async def after_commit(self) -> None:
        self.log.append(f"after_commit(active={is_transaction_active()})")
        if self.fail_after_commit:
            raise RuntimeError("after-commit side effect failed")

    async def after_completion(self, status: CompletionStatus) -> None:
        self.log.append(f"after_completion({status.value})")


class TestSynchronizations:
    async def test_callbacks_run_in_order_around_the_commit(self, app: App) -> None:
        log: list[str] = []
        items = app.bean(TxItemRepository)

        @transactional
        async def work() -> None:
            register_synchronization(Recorder(log))
            await after_commit(lambda: log.append("after_commit callback"))
            await items.save(TxItem(name="synced"))

        await work()
        assert log == [
            "before_commit(read_only=False, active=True)",
            "before_completion",
            "after_commit(active=False)",
            "after_commit callback",
            "after_completion(COMMITTED)",
        ]

    async def test_after_commit_does_not_run_on_rollback(self, app: App) -> None:
        log: list[str] = []

        @transactional
        async def work() -> None:
            register_synchronization(Recorder(log))
            await after_commit(lambda: log.append("never"))
            raise ValueError("rolled back")

        with pytest.raises(ValueError):
            await work()
        assert log == ["before_completion", "after_completion(ROLLED_BACK)"]

    async def test_after_commit_runs_at_once_outside_a_transaction(self) -> None:
        log: list[str] = []
        await after_commit(lambda: log.append("now"))
        assert log == ["now"]

    async def test_register_synchronization_needs_a_unit(self) -> None:
        with pytest.raises(IllegalTransactionStateError):
            register_synchronization(Recorder([]))

    async def test_a_failing_after_commit_is_logged_and_counted_not_raised(
        self, app: App, caplog: pytest.LogCaptureFixture
    ) -> None:
        from prometheus_client import REGISTRY

        from pyfly.data.transaction import installed_registry
        from pyfly.observability.metrics import MetricsRegistry

        installed = installed_registry()
        assert installed is not None
        installed.metrics = MetricsRegistry()
        labels = {"datasource": "primary", "phase": "after_commit"}
        before = REGISTRY.get_sample_value("pyfly_tx_synchronization_failures_total", labels) or 0.0
        items = app.bean(TxItemRepository)

        @transactional
        async def work() -> None:
            register_synchronization(Recorder([], fail_after_commit=True))
            await items.save(TxItem(name="committed-anyway"))

        with caplog.at_level(logging.ERROR):
            await work()
        assert await app.items() == ["committed-anyway"]
        assert "transaction_synchronization_failed" in caplog.text
        assert REGISTRY.get_sample_value("pyfly_tx_synchronization_failures_total", labels) == before + 1

    async def test_repository_calls_in_after_commit_get_their_own_units(self, app: App) -> None:
        items = app.bean(TxItemRepository)
        audits = app.bean(TxAuditRepository)

        @transactional
        async def work() -> None:
            await items.save(TxItem(name="order"))
            await after_commit(lambda: audits.save(TxAudit(name="published-after-commit")))

        await work()
        assert await app.audits() == ["published-after-commit"]

    async def test_a_failing_before_commit_rolls_back(self, app: App) -> None:
        items = app.bean(TxItemRepository)

        class Veto(TransactionSynchronizationAdapter):
            async def before_commit(self, read_only: bool) -> None:
                raise PermissionError("vetoed")

        @transactional
        async def work() -> None:
            register_synchronization(Veto())
            await items.save(TxItem(name="vetoed"))

        with pytest.raises(PermissionError):
            await work()
        assert await app.items() == []

    async def test_current_unit_of_work_inside_and_outside(self, app: App) -> None:
        assert current_unit_of_work() is None

        @transactional(name="labeled")
        async def work() -> str:
            unit = current_unit_of_work("primary")
            assert unit is not None
            return unit.describe()

        assert "labeled" in await work()
