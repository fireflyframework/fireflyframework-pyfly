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
"""Savepoints opened from concurrent child tasks of one unit fail loudly (WP01-05, WP01-10).

Savepoints are a stack on the unit's one connection. Child tasks that share a unit (``asyncio.gather``
inside ``@transactional``) interleave on it, so a savepoint one child opens would sit on top of another
child's, and a statement of the second child would run inside the first child's savepoint: a
``ROLLBACK TO SAVEPOINT`` of either would undo the other's work after it reported success, and the unit
would commit without it. The unit refuses instead, with ``IllegalTransactionStateError``:

- ``gather`` over ``Propagation.NESTED`` steps that await something after their write, in the order
  ok, duplicate, ok: one step runs, the others are refused;
- ``gather`` over the ``async with session.begin_nested():`` idiom;
- a plain repository write from a sibling while a ``NESTED`` step holds its savepoint;
- a ``NESTED`` scope that ends while a child task it started still holds a savepoint on top of its own:
  it neither releases nor rolls back across that savepoint, and the unit rolls back;
- a unit whose boundary ends while a child task it started still holds a savepoint on it (a
  ``@transactional`` method, or a repository call's own unit, that returns without awaiting the child): it
  does not commit, which would release that savepoint under the child and commit the child's work whatever
  the child does next; it rolls back and raises instead;
- a ``NESTED`` step whose unit ended under it fails with ``IllegalTransactionStateError`` whether it
  returns or raises: its work went with the unit, not with its savepoint.

Every step's reported outcome matches the committed rows: a step never reports success while its row is
missing. Children of a task that holds a savepoint (fan-out inside a ``NESTED`` scope) still work inside
it, and the unit commits their writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import IllegalTransactionStateError
from tests.support.backend_matrix import MARIADB, MYSQL, PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG, MYSQL)


class CsItem(Base):
    __tablename__ = "cs_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


@repository
class CsItemRepository(Repository[CsItem, int]):
    child: asyncio.Task[None] | None = None

    async def insert_ignoring(self, name: str, pause: float) -> bool:
        """The SQLAlchemy savepoint idiom, with an await inside the block (an external call)."""
        session = self._session
        try:
            async with session.begin_nested():
                session.add(CsItem(name=name))
                await asyncio.sleep(pause)
        except IntegrityError:
            return False
        return True

    async def return_under_a_savepoint_child(self, holding: asyncio.Event, release: asyncio.Event) -> None:
        """A method of the application's own (not atomic) whose child task holds a savepoint as it returns."""
        session = self._session

        async def child() -> None:
            async with session.begin_nested():
                session.add(CsItem(name="child"))
                await session.flush()
                holding.set()
                await release.wait()

        await self.save(CsItem(name="seed"))
        self.child = asyncio.create_task(child())
        await holding.wait()


@service
class CsStep:
    def __init__(self, items: CsItemRepository) -> None:
        self.items = items
        self.child: asyncio.Task[str] | None = None

    @transactional(propagation=Propagation.NESTED)
    async def insert(self, name: str, pause: float) -> str:
        await self.items.save(CsItem(name=name))
        await asyncio.sleep(pause)  # an external call after the write
        return name

    @transactional(propagation=Propagation.NESTED)
    async def insert_then_fail(self, name: str, pause: float) -> str:
        await self.items.save(CsItem(name=name))
        await asyncio.sleep(pause)
        raise ValueError(f"{name} failed after its write")

    @transactional(propagation=Propagation.NESTED)
    async def insert_and_hold(self, name: str, holding: asyncio.Event, release: asyncio.Event) -> str:
        await self.items.save(CsItem(name=name))
        holding.set()
        await release.wait()
        return name

    @transactional(propagation=Propagation.NESTED)
    async def insert_hold_then_fail(self, name: str, holding: asyncio.Event, release: asyncio.Event) -> str:
        await self.items.save(CsItem(name=name))
        holding.set()
        await release.wait()
        raise ValueError(f"{name} failed after its write")

    @transactional(propagation=Propagation.NESTED)
    async def fan_out_inside(self, names: Sequence[str]) -> list[str]:
        await asyncio.gather(*(self.items.save(CsItem(name=name)) for name in names[:-1]))
        await asyncio.create_task(self.insert(names[-1], 0))  # one child, one NESTED step on top of ours
        return list(names)

    @transactional(propagation=Propagation.NESTED)
    async def leave_a_nested_child_running(self, holding: asyncio.Event, release: asyncio.Event) -> None:
        await self.items.save(CsItem(name="parent"))
        self.child = asyncio.create_task(self.insert_and_hold("child", holding, release))
        await holding.wait()  # the child's savepoint is open on top of this scope's


@service
class CsOuter:
    def __init__(self, items: CsItemRepository, step: CsStep) -> None:
        self.items = items
        self.step = step
        self.child: asyncio.Task[str] | None = None

    @transactional
    async def gather_nested(self, steps: Sequence[tuple[str, float]]) -> list[object]:
        await self.items.save(CsItem(name="seed"))
        return await asyncio.gather(*(self.step.insert(name, pause) for name, pause in steps), return_exceptions=True)

    @transactional
    async def gather_savepoint_idiom(self, steps: Sequence[tuple[str, float]]) -> list[object]:
        await self.items.save(CsItem(name="seed"))
        return await asyncio.gather(
            *(self.items.insert_ignoring(name, pause) for name, pause in steps), return_exceptions=True
        )

    @transactional
    async def gather_a_failing_nested_step_and_a_plain_write(self) -> list[object]:
        await self.items.save(CsItem(name="seed"))

        async def plain_write(name: str) -> str:
            await self.items.save(CsItem(name=name))
            return name

        return await asyncio.gather(
            self.step.insert_then_fail("nested", 0.01), plain_write("plain"), return_exceptions=True
        )

    @transactional
    async def fan_out_inside_a_nested_scope(self) -> list[str]:
        await self.items.save(CsItem(name="seed"))
        return await self.step.fan_out_inside(["a", "b", "c"])

    @transactional
    async def end_a_nested_scope_under_a_running_child(self, holding: asyncio.Event, release: asyncio.Event) -> None:
        await self.items.save(CsItem(name="seed"))
        await self.step.leave_a_nested_child_running(holding, release)

    @transactional
    async def return_under_a_nested_child(
        self, child_fails: bool, holding: asyncio.Event, release: asyncio.Event
    ) -> None:
        await self.items.save(CsItem(name="seed"))
        step = self.step.insert_hold_then_fail if child_fails else self.step.insert_and_hold
        self.child = asyncio.create_task(step("child", holding, release))
        await holding.wait()  # the child's NESTED savepoint is open: this unit completes under it

    @transactional
    async def fail_under_a_nested_child(self, holding: asyncio.Event, release: asyncio.Event) -> None:
        await self.items.save(CsItem(name="seed"))
        self.child = asyncio.create_task(self.step.insert_hold_then_fail("child", holding, release))
        await holding.wait()
        raise ValueError("the unit failed")


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def outer(self) -> CsOuter:
        return self.ctx.get_bean(CsOuter)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM cs_item ORDER BY name"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(CsItem)
    ctx = ApplicationContext(relational_backend.config())
    for bean in (RelationalAutoConfiguration, CsItemRepository, CsStep, CsOuter):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        started = Harness(relational_backend, ctx)
        yield started
        assert started.checked_out() == 0
    finally:
        await ctx.stop()


def _assert_outcomes_match_the_rows(names: Sequence[str], outcomes: Sequence[object], committed: list[str]) -> None:
    """A step that reported success has its row committed; a step that failed has none of its own."""
    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            assert name == "seed" or name not in committed, f"{name!r} failed ({outcome!r}) but was committed"
        elif outcome is False:
            assert name == "seed", f"{name!r} reported a duplicate"
        else:
            assert name in committed, f"{name!r} reported success ({outcome!r}) but is not committed: {committed}"


_OK_DUP_OK = [("ok", 0.01), ("seed", 0.0), ("ok2", 0.01)]


async def test_gather_over_nested_steps_refuses_the_steps_that_would_interleave(harness: Harness) -> None:
    outcomes = await harness.outer.gather_nested(_OK_DUP_OK)
    committed = await harness.committed()
    _assert_outcomes_match_the_rows([name for name, _ in _OK_DUP_OK], outcomes, committed)
    assert outcomes[0] == "ok"
    assert all(isinstance(outcome, IllegalTransactionStateError) for outcome in outcomes[1:]), outcomes
    assert "savepoint" in str(outcomes[1])
    assert committed == ["ok", "seed"]


async def test_gather_over_nested_steps_without_a_pause_stays_consistent(harness: Harness) -> None:
    steps = [("ok", 0.0), ("seed", 0.0), ("ok2", 0.0)]
    outcomes = await harness.outer.gather_nested(steps)
    committed = await harness.committed()
    _assert_outcomes_match_the_rows([name for name, _ in steps], outcomes, committed)
    assert outcomes[0] == "ok"
    assert "seed" in committed


async def test_gather_over_the_savepoint_idiom_refuses_the_blocks_that_would_interleave(harness: Harness) -> None:
    steps = [("b", 0.01), ("seed", 0.0), ("c", 0.01), ("d", 0.0)]
    outcomes = await harness.outer.gather_savepoint_idiom(steps)
    committed = await harness.committed()
    _assert_outcomes_match_the_rows([name for name, _ in steps], outcomes, committed)
    assert outcomes[0] is True
    assert all(isinstance(outcome, IllegalTransactionStateError) for outcome in outcomes[1:]), outcomes
    assert committed == ["b", "seed"]


async def test_a_plain_write_is_refused_inside_a_siblings_savepoint(harness: Harness) -> None:
    outcomes = await harness.outer.gather_a_failing_nested_step_and_a_plain_write()
    committed = await harness.committed()
    _assert_outcomes_match_the_rows(["nested", "plain"], outcomes, committed)
    assert isinstance(outcomes[0], ValueError)
    assert isinstance(outcomes[1], IllegalTransactionStateError), outcomes
    assert committed == ["seed"]


async def test_children_of_a_nested_scope_work_inside_its_savepoint(harness: Harness) -> None:
    assert await harness.outer.fan_out_inside_a_nested_scope() == ["a", "b", "c"]
    assert await harness.committed() == ["a", "b", "c", "seed"]


async def test_a_nested_scope_never_ends_across_a_savepoint_a_running_child_holds(harness: Harness) -> None:
    holding, release = asyncio.Event(), asyncio.Event()
    with pytest.raises(IllegalTransactionStateError, match="still holds a savepoint on top of the scope's"):
        await harness.outer.end_a_nested_scope_under_a_running_child(holding, release)
    assert await harness.committed() == []
    release.set()
    child = harness.ctx.get_bean(CsStep).child
    assert child is not None
    with pytest.raises(IllegalTransactionStateError, match="already rolled_back"):
        await child  # its unit rolled back while it held its savepoint: it reports a failure, not success
    assert await harness.committed() == []


async def _outcome_of(child: asyncio.Task[object] | None, release: asyncio.Event) -> object:
    """Let *child* go on and return what it ended with (its result or its exception)."""
    release.set()
    assert child is not None
    try:
        return await child
    except Exception as error:  # noqa: BLE001 — the outcome is what the test checks
        return error


@pytest.mark.backends(SQLITE_FILE, PG, MYSQL, MARIADB)
@pytest.mark.parametrize("child_fails", [False, True], ids=["child-returns", "child-raises"])
async def test_a_unit_never_commits_under_a_savepoint_a_running_child_holds(
    harness: Harness, child_fails: bool
) -> None:
    holding, release = asyncio.Event(), asyncio.Event()
    try:
        with pytest.raises(IllegalTransactionStateError, match="cannot commit: task .* still holds a savepoint"):
            await harness.outer.return_under_a_nested_child(child_fails, holding, release)
        assert await harness.committed() == []
    finally:
        outcome = await _outcome_of(harness.outer.child, release)
    # Its unit rolled back under its savepoint: the child reports a failure whether it returns or raises.
    assert isinstance(outcome, IllegalTransactionStateError), repr(outcome)
    assert "already rolled_back" in str(outcome)
    if child_fails:
        assert isinstance(outcome.__context__, ValueError)
    assert await harness.committed() == []


@pytest.mark.backends(SQLITE_FILE, PG, MYSQL, MARIADB)
async def test_a_nested_step_that_fails_after_its_unit_ended_fails_loudly(harness: Harness) -> None:
    holding, release = asyncio.Event(), asyncio.Event()
    try:
        with pytest.raises(ValueError, match="the unit failed"):
            await harness.outer.fail_under_a_nested_child(holding, release)
    finally:
        outcome = await _outcome_of(harness.outer.child, release)
    # Its ROLLBACK TO SAVEPOINT cannot run on a completed unit: the step says so instead of reporting its own
    # failure as if its savepoint had undone its work.
    assert isinstance(outcome, IllegalTransactionStateError), repr(outcome)
    assert "already rolled_back" in str(outcome)
    assert isinstance(outcome.__context__, ValueError)
    assert await harness.committed() == []


@pytest.mark.backends(SQLITE_FILE, PG, MYSQL, MARIADB)
async def test_a_repository_call_never_commits_its_unit_under_a_childs_savepoint(harness: Harness) -> None:
    items = harness.ctx.get_bean(CsItemRepository)
    holding, release = asyncio.Event(), asyncio.Event()
    try:
        with pytest.raises(IllegalTransactionStateError, match="cannot commit: task .* still holds a savepoint"):
            await items.return_under_a_savepoint_child(holding, release)
        assert await harness.committed() == []
    finally:
        outcome = await _outcome_of(items.child, release)
    assert isinstance(outcome, IllegalTransactionStateError), repr(outcome)
    assert await harness.committed() == []
