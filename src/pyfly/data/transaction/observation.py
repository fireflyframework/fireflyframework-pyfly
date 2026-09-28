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
"""Whether a block of code committed anything: :func:`track_commits`.

Code that runs work it does not control (an orchestration engine running a saga step, a TCC participant or a
workflow step) must know whether that work committed before it failed, timed out or was cancelled. A step
that committed has effects: it is compensated (or cancelled, in TCC), and it is never retried blindly, since
a retry would apply its writes twice. A step that rolled back has none. The call's exception cannot tell:
commits are shielded, so a cancellation or a timeout that lands while ``COMMIT`` is in flight lets the commit
finish and is raised afterwards, from a call whose work did commit::

    with track_commits() as commits:
        try:
            await step()
        except BaseException:
            if commits.may_have_committed:   # compensate it, never retry it
                ...
            raise

Every write unit of work that begins inside the block reports how it ended to the tracker once it has
completed, before the call that owns it returns or raises:

- a new transaction a boundary opens (``@transactional``, ``TransactionTemplate``, ``REQUIRES_NEW``, a
  ``NESTED`` call with no unit to nest in) that is not read-only;
- the write auto unit of a repository call made outside a transaction, and the short unit
  :func:`~pyfly.data.transaction.infrastructure_unit` opens for a framework adapter (a database-backed
  cache, the outbox) outside one.

Read-only units and read auto units report nothing: they commit nothing. Neither does a call that joins a
unit opened before the block (a participant, a ``NESTED`` savepoint): that unit's own boundary commits it,
outside the block. Units begun by tasks started inside the block report too (a child task inherits the
tracker), except :func:`~pyfly.data.transaction.detached` work, which starts without one: its commits are
its own. Nested blocks each see the units of the innermost one.

A write auto unit on an autocommit connection (a single-statement :func:`~pyfly.data.transaction.infrastructure_unit`
on PostgreSQL) commits each statement as it runs: one that ran a statement and then failed, or was cancelled,
reports ``UNKNOWN`` (it may have committed), never rolled back.

Only units of work the framework manages are seen: a session opened straight from an ``async_sessionmaker``
and committed by hand, or a raw connection, is invisible here (and its commit is not shielded either).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from pyfly.data.transaction.context import COMMIT_TRACKERS
from pyfly.data.transaction.synchronization import CompletionStatus, TransactionSynchronizationAdapter

if TYPE_CHECKING:
    from pyfly.data.transaction.unit_of_work import UnitOfWork


class CommitTracker:
    """How the write units of work begun inside a :func:`track_commits` block ended, counted by outcome."""

    __slots__ = ("committed", "datasources", "rolled_back", "unknown")

    def __init__(self) -> None:
        self.committed = 0
        """Units that committed."""
        self.rolled_back = 0
        """Units that rolled back (a failed commit included)."""
        self.unknown = 0
        """Units whose ``COMMIT`` was interrupted in flight (``CommitOutcomeUnknownError``): they may have
        committed."""
        self.datasources: list[str] = []
        """The datasource of each unit that committed or may have, in the order they completed."""

    @property
    def may_have_committed(self) -> bool:
        """Whether any unit committed, or may have: the block's work has effects to compensate."""
        return self.committed > 0 or self.unknown > 0

    def record(self, datasource: str, status: CompletionStatus) -> None:
        """Count one unit of *datasource* that ended with *status*."""
        if status is CompletionStatus.COMMITTED:
            self.committed += 1
        elif status is CompletionStatus.UNKNOWN:
            self.unknown += 1
        else:
            self.rolled_back += 1
            return
        self.datasources.append(datasource)

    def __repr__(self) -> str:
        return f"CommitTracker(committed={self.committed}, rolled_back={self.rolled_back}, unknown={self.unknown})"


class _Report(TransactionSynchronizationAdapter):
    """Reports the outcome of one unit to the trackers open where it began."""

    __slots__ = ("_datasource", "_trackers")

    def __init__(self, trackers: tuple[CommitTracker, ...], datasource: str) -> None:
        self._trackers = trackers
        self._datasource = datasource

    async def after_completion(self, status: CompletionStatus) -> None:
        for tracker in self._trackers:
            tracker.record(self._datasource, status)


@contextlib.contextmanager
def track_commits() -> Iterator[CommitTracker]:
    """Track how the write units of work begun inside the block end (see the module documentation).

    The block may be exited before a unit it began completes (a task it started outlives it): that unit still
    reports to the tracker when it completes.
    """
    tracker = CommitTracker()
    token = COMMIT_TRACKERS.set((*COMMIT_TRACKERS.get(), tracker))
    try:
        yield tracker
    finally:
        COMMIT_TRACKERS.reset(token)


def observe(unit: UnitOfWork) -> None:
    """Have *unit*, a unit of work that just began, report its outcome to the trackers open in the running
    task, unless it is read-only. Called by the template for every unit it begins."""
    trackers = COMMIT_TRACKERS.get()
    if trackers and not unit.read_only:
        unit.synchronizations.append(_Report(trackers, unit.datasource))
