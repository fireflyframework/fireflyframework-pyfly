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
"""Statement counter: what an engine really sends to the database, for assertions in tests.

A repository method's cost is the number of statements it sends, and that cost is invisible in a
test that only checks results: ``save()`` sending an INSERT plus a SELECT, or ``save_all(100)`` sending
100 SELECTs, passes every functional test (C161, F11). :class:`StatementCounter` records every
statement SQLAlchemy hands to the DBAPI cursor, through the engine's ``before_cursor_execute`` event,
together with the transaction completions (``commit`` / ``rollback``) SQLAlchemy performs::

    from pyfly.testing import StatementCounter

    with StatementCounter(engine) as counter:
        await repo.save_all(entities)

    assert counter.counts() == {"INSERT": 1}
    assert counter.commits == 1

It works with an ``AsyncEngine`` or a sync ``Engine``, and counts everything the engine sends while the
counter is active, whichever session or connection sends it. What it counts:

- One entry per cursor execution. An ``executemany`` batch, or one of SQLAlchemy's "insertmanyvalues"
  batches, is one entry (``executemany`` tells them apart), because it is one round trip.
- The verb is the statement's first keyword, upper-cased, after leading comments and parentheses:
  ``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``, ``WITH``, ``PRAGMA``, ``SAVEPOINT``...
- ``BEGIN`` is not a cursor execution on most drivers (the DBAPI starts transactions implicitly, and
  asyncpg starts them through its own API), so it does not appear unless something executes it as SQL.
- :attr:`commits` and :attr:`rollbacks` count SQLAlchemy's commit and rollback of a connection. On an
  ``AUTOCOMMIT`` connection they still fire, but the driver sends nothing.

Importing this module does not import SQLAlchemy; :meth:`StatementCounter.start` does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine

# Leading whitespace, "--" line comments, "/* */" block comments and opening parentheses, in any order.
_PREAMBLE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/|\()*", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")


def statement_verb(sql: str) -> str:
    """The first SQL keyword of *sql*, upper-cased (``""`` when there is none)."""
    preamble = _PREAMBLE.match(sql)
    match = _FIRST_WORD.match(sql, preamble.end() if preamble else 0)
    return match.group(0).upper() if match else ""


@dataclass(frozen=True, slots=True)
class RecordedStatement:
    """One cursor execution: its verb, the SQL text, the parameters and whether it was an executemany."""

    verb: str
    sql: str
    parameters: Any
    executemany: bool


class StatementCounter:
    """Record the statements an engine sends while the counter is active.

    Use it as a context manager, or call :meth:`start` and :meth:`stop`. A stopped counter keeps what
    it recorded; :meth:`reset` clears it.
    """

    def __init__(self, engine: AsyncEngine | Engine) -> None:
        # An AsyncEngine exposes its events through the sync engine it wraps.
        self._engine: Engine = cast("Engine", getattr(engine, "sync_engine", None) or engine)
        self._statements: list[RecordedStatement] = []
        self._commits = 0
        self._rollbacks = 0
        self._active = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> Self:
        """Attach the listeners to the engine (idempotent)."""
        if not self._active:
            from sqlalchemy import event

            event.listen(self._engine, "before_cursor_execute", self._on_cursor_execute)
            event.listen(self._engine, "commit", self._on_commit)
            event.listen(self._engine, "rollback", self._on_rollback)
            self._active = True
        return self

    def stop(self) -> None:
        """Detach the listeners; what was recorded stays available (idempotent)."""
        if self._active:
            from sqlalchemy import event

            event.remove(self._engine, "before_cursor_execute", self._on_cursor_execute)
            event.remove(self._engine, "commit", self._on_commit)
            event.remove(self._engine, "rollback", self._on_rollback)
            self._active = False

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def reset(self) -> None:
        """Forget everything recorded so far (the counter stays attached if it was)."""
        self._statements.clear()
        self._commits = 0
        self._rollbacks = 0

    @property
    def active(self) -> bool:
        """Whether the listeners are attached."""
        return self._active

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    @property
    def statements(self) -> tuple[RecordedStatement, ...]:
        """Every recorded statement, in the order it was sent."""
        return tuple(self._statements)

    @property
    def commits(self) -> int:
        """How many connection commits SQLAlchemy performed."""
        return self._commits

    @property
    def rollbacks(self) -> int:
        """How many connection rollbacks SQLAlchemy performed."""
        return self._rollbacks

    def verbs(self) -> list[str]:
        """The verb of every statement, in order, e.g. ``["INSERT", "SELECT"]``."""
        return [statement.verb for statement in self._statements]

    def counts(self) -> dict[str, int]:
        """Statements per verb, in order of first appearance, e.g. ``{"INSERT": 1, "SELECT": 100}``."""
        counts: dict[str, int] = {}
        for statement in self._statements:
            counts[statement.verb] = counts.get(statement.verb, 0) + 1
        return counts

    def count(self, verb: str | None = None) -> int:
        """How many statements were sent, in total or for one *verb* (case-insensitive)."""
        if verb is None:
            return len(self._statements)
        wanted = verb.upper()
        return sum(1 for statement in self._statements if statement.verb == wanted)

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def _on_cursor_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self._statements.append(RecordedStatement(statement_verb(statement), statement, parameters, executemany))

    def _on_commit(self, conn: Any) -> None:
        self._commits += 1

    def _on_rollback(self, conn: Any) -> None:
        self._rollbacks += 1
