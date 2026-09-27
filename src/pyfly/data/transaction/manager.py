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
"""The transaction manager SPI: what a backend implements so the template can run units on it.

A :class:`TransactionManager` serves one datasource. It opens units of work (:meth:`begin` for a
transaction, :meth:`open_auto_unit` for the short unit a repository call gets outside a transaction),
completes them (:meth:`commit`, :meth:`rollback`, :meth:`release`), and manages savepoints for
``NESTED``. It never decides propagation, rollback rules, synchronizations or cancellation handling:
:class:`~pyfly.data.transaction.template.TransactionTemplate` does, once, for every backend.

The adapters are :class:`pyfly.data.relational.sqlalchemy.transaction_manager.SqlAlchemyTransactionManager`
(one per registry datasource) and the MongoDB manager of the document module.

A manager whose backend lets the application open savepoints of its own may also define
``failed_within_savepoint(unit, savepoint) -> bool``: whether a statement failed inside such a savepoint,
opened within a ``NESTED`` scope's *savepoint* and left open. The template then rolls the ``NESTED`` scope
back to its savepoint. It is optional, and not part of the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pyfly.data.transaction.definition import Isolation, TransactionDefinition
from pyfly.data.transaction.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class TransactionCapabilities:
    """What a transaction manager's backend supports.

    ``isolation_levels`` are the levels it accepts (``"SERIALIZABLE"``, ``"READ COMMITTED"``...);
    ``fast_autocommit_reads`` says a single-statement read is cheaper on an autocommit connection than in
    a transaction (PostgreSQL). ``multiple_active_results`` says another operation can run on a unit's
    resource while a streamed result is open on it; where it cannot (MySQL, MariaDB), the backend records
    each open stream on its unit (:meth:`~pyfly.data.transaction.unit_of_work.UnitOfWork.stream_opened`),
    and the unit refuses every other operation until the stream is done.
    """

    backend: str
    supports_savepoints: bool
    isolation_levels: frozenset[str] = field(default_factory=frozenset)
    fast_autocommit_reads: bool = False
    multiple_active_results: bool = True

    def supports_isolation(self, isolation: Isolation) -> bool:
        """Whether *isolation* can be applied (``DEFAULT`` always can)."""
        return isolation is Isolation.DEFAULT or isolation.value in self.isolation_levels


@runtime_checkable
class TransactionManager(Protocol):
    """Runs units of work on one datasource. Backends implement it; the template drives it."""

    @property
    def datasource(self) -> str:
        """The name of the datasource this manager serves (the key units are bound under)."""
        ...

    @property
    def capabilities(self) -> TransactionCapabilities:
        """What the backend supports (savepoints, isolation levels)."""
        ...

    async def begin(self, definition: TransactionDefinition) -> UnitOfWork:
        """Open a new transaction for *definition* (isolation, read-only, timeout applied) and return its
        unit, not yet bound. Raise :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError`
        for a setting the backend cannot honor."""
        ...

    async def open_auto_unit(self, *, read_only: bool, autocommit: bool | None = None) -> UnitOfWork:
        """Open the short unit a call gets outside a transaction: a read unit or a write unit that will
        commit. *autocommit* ``None`` lets the backend run a read on an autocommit connection where that is
        cheaper; ``True`` asks for it for a single statement that writes; ``False`` refuses it (a
        server-side cursor needs a transaction)."""
        ...

    async def commit(self, unit: UnitOfWork) -> None:
        """Commit *unit*. A failure while the commit was in flight raises
        :class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`."""
        ...

    async def rollback(self, unit: UnitOfWork) -> None:
        """Roll *unit* back (a poisoned unit's connection is discarded instead)."""
        ...

    async def release(self, unit: UnitOfWork) -> None:
        """Release *unit*'s resource (close the session and return the connection)."""
        ...

    async def create_savepoint(self, unit: UnitOfWork) -> Any:
        """Open a savepoint in *unit* and return a handle for it.

        Open it under the unit's operation guard (``async with unit.operation():``), which refuses it while
        another task holds the unit's innermost savepoint, and record it there with
        ``unit.savepoint_opened(handle)``, so no other task's statement lands in it before it is recorded.
        Report each savepoint that ends, however it ends, with ``unit.savepoint_closed(handle)``. The
        template records a ``NESTED`` scope's savepoint and forgets it at the scope's end itself when a
        manager does not."""
        ...

    async def release_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """Release (commit) *savepoint*. When that fails, the template rolls back to *savepoint* (which must
        still be usable for that) and hands the failure to the ``NESTED`` scope's caller."""
        ...

    async def rollback_to_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """Roll *unit* back to *savepoint*."""
        ...

    def resource_active(self, unit: UnitOfWork) -> bool:
        """Whether *unit*'s transaction can still commit (a failed flush deactivates it)."""
        ...

    def marks_rollback_only(self, unit: UnitOfWork, error: Exception) -> bool:
        """Whether *error*, raised by an operation on *unit*'s resource, leaves the transaction unusable.

        A backend may also set ``unit.poisoned`` when the error leaves the connection in an unknown state;
        a poisoned unit's connection is discarded instead of rolled back.
        """
        ...

    def is_disconnect(self, error: BaseException) -> bool:
        """Whether *error* means the connection was lost (a read auto unit retries once on it)."""
        ...
