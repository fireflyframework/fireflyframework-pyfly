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
"""A gate that holds a real ``COMMIT`` in flight, on the sqlite-file and PostgreSQL lanes.

Tests of what happens when a cancellation, a timeout or a sibling's failure lands while a unit of work is
committing need the commit to be in flight for as long as the test wants, with the database really
applying it afterwards. Nothing is mocked: the server (or SQLite's lock manager) makes the ``COMMIT`` wait.

- **PostgreSQL.** A deferred constraint trigger on the gated table runs at ``COMMIT`` and takes a
  transaction-level advisory lock. While the gate is closed, a connection of its own holds that lock, so a
  transaction that wrote to the table waits inside its ``COMMIT`` until the gate opens, and then commits.
- **sqlite-file.** The database runs in rollback-journal mode (``pyfly.data.relational.sqlite.journal-mode:
  DELETE``, see :data:`SQLITE_OVERRIDES`): a ``COMMIT`` needs the exclusive lock, and while the gate is
  closed a connection of its own holds a shared lock, so the writer waits in SQLite's busy handler (on
  aiosqlite's worker thread; the event loop stays free) until the gate opens, and then commits. Open the
  gate within ``busy-timeout`` (5 s by default), or the commit fails with "database is locked".

:attr:`CommitGate.commit_sent` is set when a ``COMMIT`` starts on the application's engine while the gate is
closed, so a test acts once the commit is in flight, never after a fixed delay.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

SQLITE_OVERRIDES: dict[str, Any] = {"pyfly.data.relational.sqlite.journal-mode": "DELETE"}
"""The configuration a sqlite-file application needs for the gate: rollback-journal mode, where a commit
waits for readers (in WAL mode it does not)."""

GATED_LANES: tuple[str, ...] = (SQLITE_FILE, PG)
"""The lanes a gate works on (MySQL and MariaDB have no deferred trigger to hold a commit with)."""


class CommitGate:
    """Holds every ``COMMIT`` of a transaction that wrote to *table* in flight while it is closed.

    Use it as an async context manager (``async with CommitGate(backend, engine, "t") as gate:``); it is open
    when entered. :meth:`close` holds the next commits, :meth:`open` lets them finish.
    """

    def __init__(self, backend: RelationalBackend, engine: AsyncEngine, table: str) -> None:
        if backend.lane not in GATED_LANES:
            raise ValueError(f"a commit gate needs one of the lanes {GATED_LANES}, not {backend.lane}")
        self._backend = backend
        self._engine = engine
        self._table = table
        self._key = uuid.uuid4().int % 2_000_000_000
        self._closed = False
        self._pg: AsyncEngine | None = None
        self._pg_connection: AsyncConnection | None = None
        self._sqlite: sqlite3.Connection | None = None
        self.commit_sent = asyncio.Event()
        """Set when a ``COMMIT`` starts on the application's engine while the gate is closed."""

    async def __aenter__(self) -> CommitGate:
        event.listen(self._engine.sync_engine, "commit", self._on_commit)
        if self._backend.lane == PG:
            await self._install_trigger()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.open()
        event.remove(self._engine.sync_engine, "commit", self._on_commit)
        if self._pg is not None:
            await self._pg.dispose()

    def _on_commit(self, _connection: Any) -> None:
        if self._closed:
            self.commit_sent.set()

    async def _install_trigger(self) -> None:
        self._pg = create_async_engine(self._backend.url, poolclass=NullPool)
        function = f"pyfly_commit_gate_{self._key}"
        async with self._pg.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$ "
                    f"BEGIN PERFORM pg_advisory_xact_lock({self._key}); RETURN NULL; END $$ LANGUAGE plpgsql"
                )
            )
            await conn.execute(
                text(
                    f"CREATE CONSTRAINT TRIGGER {function} AFTER INSERT OR UPDATE OR DELETE ON {self._table} "
                    f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {function}()"
                )
            )

    async def close(self) -> None:
        """Hold the commits that start from now on until :meth:`open`."""
        if self._closed:
            return
        self.commit_sent.clear()
        if self._backend.lane == PG:
            assert self._pg is not None
            self._pg_connection = await self._pg.connect()
            await self._pg_connection.execute(text(f"SELECT pg_advisory_lock({self._key})"))
        else:
            database = make_url(self._backend.url).database
            assert database is not None
            self._sqlite = sqlite3.connect(database, isolation_level=None)
            self._sqlite.execute("BEGIN")
            self._sqlite.execute(f"SELECT count(*) FROM {self._table}").fetchall()  # a shared lock, kept
        self._closed = True

    async def open(self) -> None:
        """Let the commits held so far finish."""
        if not self._closed:
            return
        self._closed = False
        if self._pg_connection is not None:
            await self._pg_connection.execute(text(f"SELECT pg_advisory_unlock({self._key})"))
            await self._pg_connection.close()
            self._pg_connection = None
        if self._sqlite is not None:
            self._sqlite.execute("ROLLBACK")
            self._sqlite.close()
            self._sqlite = None

    async def wait_for_commit(self, timeout: float = 10.0) -> None:
        """Wait until a ``COMMIT`` is in flight behind the closed gate."""
        await asyncio.wait_for(self.commit_sent.wait(), timeout)
