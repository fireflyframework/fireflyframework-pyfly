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
"""Shell commands run on the application's event loop (WP13-09..12: C077, C078, C082, C088).

Async ``@shell_method`` commands used to run on a private ``asyncio.run()`` loop in a worker thread while the
engine, the pool and the sessions belonged to the application's loop: on PostgreSQL a command failed with
"attached to a different loop" (or "another operation is in progress" on a poisoned pooled connection), and
only SQLite hid it. The REPL read its input on the application's loop, so no scheduled job ran for the whole
session, and a task a command started died with the command's private loop. A real ``PyFlyApplication``
(``PyFlyApplication.run``, the CLI archetype's entry point) with a repository, a ``@transactional`` service and
a startup runner that reads the database (a warm-up) runs, on a SQLite file and on PostgreSQL:

- a one-shot command that writes, then one that reads: both succeed, their output is printed, and a failing
  command exits 1 with its message on stderr (it used to exit 1 silently);
- a REPL session (``input`` patched to wait like an operator between lines): every command succeeds and a
  ``@scheduled`` heartbeat keeps ticking between and during them;
- an ASYNC workflow a REPL command starts runs to completion while the session goes on, and one a one-shot
  command starts completes before the application exits.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import component, repository, service, shell_component
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.scheduling.decorators import scheduled
from pyfly.shell.decorators import shell_method
from pyfly.transactional.core.model import ExecutionStatus, TriggerMode
from pyfly.transactional.workflow.annotations import workflow, workflow_step
from pyfly.transactional.workflow.engine import WorkflowEngine
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class ShellItem(Base):
    __tablename__ = "wp13_shell_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class ShellItems(Repository[ShellItem, int]):
    pass


@service
class ItemService:
    def __init__(self, items: ShellItems) -> None:
        self.items = items

    @transactional
    async def add(self, name: str) -> None:
        await self.items.save(ShellItem(name=name))

    async def count(self) -> int:
        return await self.items.count()


@service
class Heartbeat:
    def __init__(self) -> None:
        self.ticks: list[float] = []

    @scheduled(fixed_rate=timedelta(seconds=0.05))
    async def beat(self) -> None:
        self.ticks.append(time.monotonic())


class Seen:
    """The beans of the last run, kept for the assertions made after the application stopped."""

    heartbeat: Heartbeat | None = None
    workflows: WorkflowEngine | None = None


@component
class Warmup:
    """A startup runner that reads the database on the application's loop (a cache warm-up)."""

    def __init__(self, service: ItemService, heartbeat: Heartbeat, workflows: WorkflowEngine) -> None:
        self.service = service
        Seen.heartbeat = heartbeat
        Seen.workflows = workflows

    async def run(self, args: list[str]) -> None:
        await self.service.count()


@workflow(id="wp13-shell-import", trigger_mode=TriggerMode.ASYNC)
class ShellImport:
    def __init__(self, service: ItemService) -> None:
        self.service = service

    @workflow_step(id="load")
    async def load(self) -> None:
        await asyncio.sleep(0.1)
        await self.service.add("imported")


@shell_component
class ItemCommands:
    started: list[str] = []

    def __init__(self, service: ItemService, workflows: WorkflowEngine) -> None:
        self.service = service
        self.workflows = workflows

    @shell_method(key="add", help="Add an item")
    async def add(self, name: str) -> str:
        await self.service.add(name)
        return f"added {name}"

    @shell_method(key="count", help="Count the items")
    async def count(self) -> str:
        return f"count={await self.service.count()}"

    @shell_method(key="fail", help="Fail")
    async def fail(self) -> str:
        raise RuntimeError("import failed: upstream down")

    @shell_method(key="import", help="Start an import in the background")
    async def start_import(self) -> str:
        result = await self.workflows.start("wp13-shell-import")
        ItemCommands.started.append(result.correlation_id)
        return f"started {result.status.value}"


@pyfly_application(name="wp13-shell")
class ShellApp:
    pass


class Session:
    def __init__(self, backend: RelationalBackend, directory: Path) -> None:
        self.backend = backend
        self.directory = directory
        self.app: PyFlyApplication | None = None

    def application(self) -> PyFlyApplication:
        app = PyFlyApplication(ShellApp, config_path=self.directory)
        for bean in (ShellItems, ItemService, Heartbeat, Warmup, ShellImport, ItemCommands):
            app.context.register_bean(bean)
        self.app = app
        return app

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT name FROM {ShellItem.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def session(relational_backend: RelationalBackend, tmp_path: Path) -> Session:
    await relational_backend.create_tables(ShellItem)
    settings: dict[str, Any] = {
        "pyfly": {
            "shell": {"enabled": True},
            "transactional": {"enabled": True},
            "banner": {"mode": "off"},
            "data": {"relational": {"enabled": True, "url": relational_backend.url, "ddl-auto": "none"}},
        }
    }
    (tmp_path / "pyfly.yaml").write_text(yaml.safe_dump(settings))
    ItemCommands.started = []
    Seen.heartbeat = Seen.workflows = None
    return Session(relational_backend, tmp_path)


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Patch ``input`` with an operator who types each queued line after 0.3 s of thought, then Ctrl-D."""
    lines: list[str] = []

    def typed(prompt: str = "") -> str:
        time.sleep(0.3)  # blocks whatever thread reads the input
        if not lines:
            raise EOFError
        return lines.pop(0)

    monkeypatch.setattr(builtins, "input", typed)
    yield lines


async def test_one_shot_commands_run_on_the_application_loop(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await session.application().run(["add", "alice"]) == 0
    assert await session.application().run(["count"]) == 0

    out = capsys.readouterr().out
    assert "added alice" in out
    assert "count=1" in out
    assert await session.committed() == ["alice"]


async def test_a_failing_one_shot_command_exits_1_with_its_message(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await session.application().run(["fail"]) == 1
    assert "import failed: upstream down" in capsys.readouterr().err


async def test_the_repl_runs_every_command_and_the_scheduler_keeps_ticking(
    session: Session, operator: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    operator.extend(["add a", "add b", "count"])
    app = session.application()
    started = time.monotonic()
    assert await app.run([]) == 0
    elapsed = time.monotonic() - started

    out = capsys.readouterr().out
    assert "added a" in out and "added b" in out and "count=2" in out
    assert await session.committed() == ["a", "b"]
    assert Seen.heartbeat is not None
    assert elapsed >= 1.2  # four prompts, 0.3 s of thought each
    assert len(Seen.heartbeat.ticks) >= 5  # it ticked through the operator's thought (it used to tick once)


async def test_an_async_workflow_started_in_the_repl_runs_while_the_session_goes_on(
    session: Session, operator: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    operator.extend(["import", "count"])
    app = session.application()
    assert await app.run([]) == 0

    out = capsys.readouterr().out
    assert "started PENDING" in out
    assert "count=1" in out  # the run completed during the operator's 0.3 s of thought
    assert await session.committed() == ["imported"]
    assert await _status(ItemCommands.started[0]) is ExecutionStatus.COMPLETED


async def test_an_async_workflow_started_by_a_one_shot_command_completes_before_exit(session: Session) -> None:
    app = session.application()
    assert await app.run(["import"]) == 0

    assert await session.committed() == ["imported"]
    assert await _status(ItemCommands.started[0]) is ExecutionStatus.COMPLETED


async def _status(correlation_id: str) -> ExecutionStatus | None:
    assert Seen.workflows is not None
    state = await Seen.workflows.get_execution(correlation_id)
    return state.status if state is not None else None
