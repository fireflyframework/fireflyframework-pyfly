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
"""The MongoDB transaction manager: units of work on one ``AsyncMongoClient``.

A :class:`MongoTransactionManager` serves one document datasource (``"document"`` by default). The
unit of work's resource is a pymongo ``AsyncClientSession``: the
:class:`~pyfly.data.transaction.template.TransactionTemplate` binds it in the unit-of-work ``ContextVar``, and
every :class:`~pyfly.data.document.mongodb.repository.MongoRepository` call passes it (``session=``) to each
Beanie and pymongo operation, so the writes of a ``@transactional`` method are part of its transaction, and
propagation (``REQUIRED``, ``REQUIRES_NEW``, ``SUPPORTS``,
``NOT_SUPPORTED``, ``MANDATORY``, ``NEVER``) works as on the relational backend. ``NESTED`` raises
:class:`~pyfly.data.transaction.errors.NestedTransactionNotSupportedError`: MongoDB has no savepoints.

- **Transactions.** A unit begins ``await session.start_transaction(...)`` with the configured read and write
  concern and, for a unit with a timeout, ``maxCommitTimeMS``. Multi-document transactions need a replica
  set or a sharded cluster: on a standalone server :meth:`MongoTransactionManager.begin` raises
  :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` saying so (the server's ``hello`` is
  asked once per manager).
- **Auto units.** A repository call outside a transaction gets a short unit: a *read* runs in a session with
  no transaction (one round trip); a *write* runs in a transaction that commits at the end of the call, so a
  method that writes several documents (``save_all``, ``delete_all_by_id``) is atomic, except when the call
  is a single-document command (``save``, ``delete``), which is atomic by itself and runs without one
  (``autocommit=True``), and on a standalone server, which has no transactions.
- **Failures.** MongoDB aborts a transaction as soon as one of its commands fails, so every driver error in
  a transactional unit marks it rollback-only: a caught duplicate key cannot commit the rest
  (:class:`~pyfly.data.transaction.errors.UnexpectedRollbackError` at the boundary). A commit whose outcome
  the driver cannot know (the ``UnknownTransactionCommitResult`` label, after pymongo's own retry) raises
  :class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`.
- **Completion.** The template runs commit, abort and ``end_session`` shielded, so a cancellation (a client
  disconnect) never leaves a transaction open on the server holding its document locks until the server's
  ``transactionLifetimeLimitSeconds``.

``@transactional`` finds the manager by ``datasource=``, by a service's legacy ``self._motor_client`` (the
manager of that client), or as the default datasource of the application. A coroutine that declares a
``session`` parameter also receives the unit's session there (:attr:`MongoTransactionManager.resource_parameter`),
for code that calls Beanie or pymongo directly::

    @transactional(datasource="document")
    async def transfer(self, source: str, target: str, amount: int, *, session=None) -> None:
        await Account.find_one(Account.id == source, session=session).inc({Account.balance: -amount}, session=session)
        ...
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from typing import Any, cast

from pymongo import AsyncMongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import AutoReconnect, PyMongoError, ServerSelectionTimeoutError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    NestedTransactionNotSupportedError,
)
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.registry import installed_registry, register_resource_resolver
from pyfly.data.transaction.template import run_shielded, shield_scope
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = ["DOCUMENT", "MongoTransactionManager", "current_session", "in_transaction"]

_logger = logging.getLogger(__name__)

DOCUMENT = "document"
"""The default name of the document datasource (``pyfly.data.document.datasource``)."""

_TRANSACTION = "pyfly_mongo_transaction"
"""``UnitOfWork.attributes`` key: the unit runs a multi-document transaction (a read auto unit, a single-command
write auto unit and every unit on a standalone server run without one)."""

_MANAGER = "_pyfly_transaction_manager"
"""The attribute an ``AsyncMongoClient`` keeps its manager in (``MongoTransactionManager.for_client``): the
manager lives exactly as long as the client."""

_LOOKUP_LOCK = threading.RLock()

_ADHOC_NAMES: weakref.WeakValueDictionary[str, MongoTransactionManager] = weakref.WeakValueDictionary()
"""The datasource names of the ad-hoc managers alive, so two clients never bind their units under one name."""

_CAPABILITIES = TransactionCapabilities(
    backend="mongodb",
    supports_savepoints=False,
    isolation_levels=frozenset(),
    fast_autocommit_reads=True,
    multiple_active_results=True,
)


def in_transaction(unit: UnitOfWork) -> bool:
    """Whether *unit* (a document unit) runs a multi-document transaction."""
    return bool(unit.attributes.get(_TRANSACTION))


def current_session(datasource: str | None = None) -> AsyncClientSession | None:
    """The ``ClientSession`` of the document unit of work the running task is in, for code that calls Beanie or
    pymongo itself (``await Document.insert(session=current_session())``): the unit of *datasource*, or the
    innermost document unit when ``None``; ``None`` outside one (the call then runs on its own)."""
    from pyfly.data.transaction.context import current_state

    state = current_state()
    if datasource is not None:
        unit = state.target(datasource)
        return (
            unit.resource
            if unit is not None and not unit.completed and isinstance(unit.manager, MongoTransactionManager)
            else None
        )
    candidates = [unit for _name, unit in reversed(state.scopes)]
    candidates += [bound for _name, bound in reversed(state.units) if isinstance(bound, UnitOfWork)]
    for unit in candidates:
        if isinstance(unit.manager, MongoTransactionManager) and not unit.completed:
            return cast(AsyncClientSession, unit.resource)
    return None


class MongoTransactionManager:
    """Runs units of work on one MongoDB client (see the module documentation).

    *datasource* is the name units are bound under. *read_concern* (``"snapshot"``, ``"majority"``, ...),
    *write_concern* (``"majority"``, a number of nodes) and *max_commit_time* (seconds) are the transaction
    options; ``None`` keeps the client's (and the server's) defaults.
    """

    resource_parameter = "session"
    """The keyword a ``@transactional`` coroutine that declares it receives the unit's resource under (the
    ``AsyncClientSession``), for code that calls Beanie or pymongo itself."""

    def __init__(
        self,
        client: AsyncMongoClient[Any],
        *,
        datasource: str = DOCUMENT,
        read_concern: str | None = None,
        write_concern: str | int | None = None,
        max_commit_time: float | None = None,
    ) -> None:
        if not isinstance(client, AsyncMongoClient):
            raise TypeError(
                f"MongoTransactionManager needs a pymongo AsyncMongoClient, got {type(client).__name__} (Motor and "
                "mongomock clients are not supported: Beanie 2 runs on pymongo's async API)"
            )
        self._client = client
        self._datasource = datasource
        self._read_concern = ReadConcern(read_concern) if read_concern else None
        self._write_concern = _write_concern(write_concern)
        self._max_commit_time = max_commit_time
        self._transactions: bool | None = None
        with _LOOKUP_LOCK:
            # The first manager built for a client is its manager (for_client) until another one is attached.
            if not isinstance(getattr(client, _MANAGER, None), MongoTransactionManager):
                setattr(client, _MANAGER, self)

    # -- factories ------------------------------------------------------------------------------------------

    @classmethod
    def for_client(cls, client: AsyncMongoClient[Any]) -> MongoTransactionManager:
        """The manager of *client*: the one the installed registry has for it, else the one kept on the client (the
        application's, :meth:`attach`; or the first one built for it), else an ad-hoc manager with the default
        settings, built once and kept on the client.

        An ad-hoc manager is named ``"document"``, unless a manager of another client already uses that name
        (the installed registry's, or another ad-hoc one): then it gets a name of its own, so a unit of one
        client is never joined by a repository of another."""
        registry = installed_registry()
        if registry is not None:
            registered = registry.find_by_resource(client)
            if isinstance(registered, MongoTransactionManager):
                return registered
        manager = getattr(client, _MANAGER, None)
        if isinstance(manager, MongoTransactionManager):
            return manager
        with _LOOKUP_LOCK:
            manager = getattr(client, _MANAGER, None)
            if not isinstance(manager, MongoTransactionManager):
                manager = cls(client, datasource=_adhoc_name(client))
                _ADHOC_NAMES[manager.datasource] = manager
            return manager

    def attach(self) -> None:
        """Make this the manager :meth:`for_client` answers for its client (the auto-configuration's manager)."""
        with _LOOKUP_LOCK:
            setattr(self._client, _MANAGER, self)

    def detach(self) -> None:
        """Undo :meth:`attach`, if this is still the client's manager."""
        with _LOOKUP_LOCK:
            if getattr(self._client, _MANAGER, None) is self:
                delattr(self._client, _MANAGER)

    # -- identity -------------------------------------------------------------------------------------------

    @property
    def datasource(self) -> str:
        """The datasource name units are bound under."""
        return self._datasource

    @property
    def client(self) -> AsyncMongoClient[Any]:
        """The client whose sessions the units run on."""
        return self._client

    @property
    def capabilities(self) -> TransactionCapabilities:
        """No savepoints and no isolation levels; reads outside a transaction run without one."""
        return _CAPABILITIES

    def owns(self, resource: object) -> bool:
        """Whether *resource* is this manager's client (a service's legacy ``_motor_client``)."""
        return resource is self._client

    async def supports_transactions(self) -> bool:
        """Whether the server runs multi-document transactions: a replica set member or a ``mongos`` (asked
        once, with ``hello``)."""
        if self._transactions is None:
            hello = await self._client.admin.command("hello")
            self._transactions = bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
        return self._transactions

    # -- opening units --------------------------------------------------------------------------------------

    async def begin(self, definition: TransactionDefinition) -> UnitOfWork:
        """Start a transaction in a new session (see the module documentation)."""
        if not await self.supports_transactions():
            raise IllegalTransactionStateError(
                f"Datasource '{self._datasource}' is a standalone MongoDB server, and multi-document transactions "
                "need a replica set or a sharded cluster. Run MongoDB as a replica set (a single-node one is enough: "
                "mongod --replSet rs0, then rs.initiate()), or drop @transactional from this call: outside a "
                "transaction each repository write is atomic on its own document. A message listener container "
                "opens a unit per delivery on the default datasource: set pyfly.messaging.listener.transactional "
                "(pyfly.eda.listener.transactional) to false to deliver without one.",
                datasource=self._datasource,
            )
        session = self._client.start_session()
        unit = UnitOfWork(self, self._datasource, session, definition=definition)
        await self._start(unit, definition)
        return unit

    async def open_auto_unit(self, *, read_only: bool, autocommit: bool | None = None) -> UnitOfWork:
        """Open the short unit of a call outside a transaction: a session, with a transaction for a write unless
        *autocommit* is ``True`` (a single-document command) or the server has none."""
        session = self._client.start_session()
        unit = UnitOfWork(self, self._datasource, session, auto=True, read_only=read_only)
        if read_only or autocommit is True:
            return unit
        try:
            transactional = await self.supports_transactions()
        except BaseException:
            await self._discard(unit)
            raise
        if transactional:
            await self._start(unit, unit.definition)
        return unit

    async def _start(self, unit: UnitOfWork, definition: TransactionDefinition) -> None:
        session: AsyncClientSession = unit.resource
        options: dict[str, Any] = {}
        if self._read_concern is not None:
            options["read_concern"] = self._read_concern
        if self._write_concern is not None:
            options["write_concern"] = self._write_concern
        commit_time = definition.timeout if definition.timeout is not None else self._max_commit_time
        if commit_time is not None:
            options["max_commit_time_ms"] = max(1, int(commit_time * 1000))
        try:
            # start_transaction is a coroutine on pymongo's async API (C041); it sends nothing: the transaction
            # starts with the unit's first command.
            with shield_scope():
                await session.start_transaction(**options)
        except BaseException:
            await self._discard(unit)
            raise
        unit.attributes[_TRANSACTION] = True

    async def _discard(self, unit: UnitOfWork) -> None:
        """End the session of a unit that failed to start (shielded; its failure is the one to raise)."""
        _result, error, _cancelled = await run_shielded(unit.resource.end_session())
        if error is not None:
            _logger.debug("unit_of_work_discard_failed", exc_info=(type(error), error, error.__traceback__))

    # -- completing units -----------------------------------------------------------------------------------

    async def commit(self, unit: UnitOfWork) -> None:
        """``commitTransaction`` (nothing for a unit without a transaction). A commit whose outcome is unknown
        raises :class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`."""
        if not in_transaction(unit):
            return
        session: AsyncClientSession = unit.resource
        try:
            await session.commit_transaction()
        except PyMongoError as error:
            if error.has_error_label("UnknownTransactionCommitResult"):
                raise CommitOutcomeUnknownError(
                    f"The commit of {unit.describe()} failed with an unknown outcome ({type(error).__name__}); the "
                    "transaction may or may not have committed. Do not retry it blindly.",
                    datasource=self._datasource,
                ) from error
            raise
        except (OSError, asyncio.CancelledError) as error:
            raise CommitOutcomeUnknownError(
                f"The commit of {unit.describe()} was interrupted in flight ({type(error).__name__}); the "
                "transaction may or may not have committed. Do not retry it blindly.",
                datasource=self._datasource,
            ) from error

    async def rollback(self, unit: UnitOfWork) -> None:
        """``abortTransaction`` (nothing for a unit without one). pymongo ignores an abort that fails: the server
        aborts an abandoned transaction on its own."""
        session: AsyncClientSession = unit.resource
        if in_transaction(unit) and session.in_transaction:
            await session.abort_transaction()

    async def release(self, unit: UnitOfWork) -> None:
        """End the session (it goes back to the client's server-session pool)."""
        await unit.resource.end_session()

    # -- savepoints: MongoDB has none ------------------------------------------------------------------------

    async def create_savepoint(self, unit: UnitOfWork) -> Any:
        """Refused: MongoDB transactions have no savepoints."""
        raise self._no_savepoints(unit)

    async def release_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """Refused: MongoDB transactions have no savepoints."""
        raise self._no_savepoints(unit)

    async def rollback_to_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """Refused: MongoDB transactions have no savepoints."""
        raise self._no_savepoints(unit)

    def _no_savepoints(self, unit: UnitOfWork) -> NestedTransactionNotSupportedError:
        return NestedTransactionNotSupportedError(
            f"MongoDB has no savepoints: {unit.describe()} cannot run Propagation.NESTED; use REQUIRES_NEW for a "
            "step that must commit or fail on its own",
            datasource=self._datasource,
        )

    # -- state ------------------------------------------------------------------------------------------------

    def resource_active(self, unit: UnitOfWork) -> bool:
        """Whether the unit can still commit: its session still runs its transaction (a unit without one
        always can)."""
        return not in_transaction(unit) or bool(unit.resource.in_transaction)

    def marks_rollback_only(self, unit: UnitOfWork, error: Exception) -> bool:
        """Every driver error in a transaction: MongoDB aborts the transaction when one of its commands fails,
        whatever the command. Outside a transaction each command stands on its own."""
        return in_transaction(unit) and isinstance(error, PyMongoError)

    def is_disconnect(self, error: BaseException) -> bool:
        """Whether *error* lost its connection (a read auto unit is retried once on it); a server that cannot be
        selected at all is not retried."""
        return isinstance(error, AutoReconnect) and not isinstance(error, ServerSelectionTimeoutError)

    def __repr__(self) -> str:
        return f"MongoTransactionManager(datasource={self._datasource!r})"


def _write_concern(value: str | int | None) -> WriteConcern | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return WriteConcern(w=int(value))
    return WriteConcern(w=str(value))


def _adhoc_name(client: AsyncMongoClient[Any]) -> str:
    """``"document"``, unless a manager of another client uses that name already (see ``for_client``)."""
    taken = _ADHOC_NAMES.get(DOCUMENT)
    registry = installed_registry()
    registered = registry.find(DOCUMENT) if registry is not None else None
    others = [
        manager
        for manager in (taken, registered)
        if manager is not None and not (callable(owns := getattr(manager, "owns", None)) and owns(client))
    ]
    return f"{DOCUMENT}-{id(client):x}" if others else DOCUMENT


def _resolve_resource(resource: object) -> MongoTransactionManager | None:
    """The resource resolver the neutral registry asks: a manager, or the manager of an ``AsyncMongoClient``."""
    if isinstance(resource, MongoTransactionManager):
        return resource
    if isinstance(resource, AsyncMongoClient):
        return MongoTransactionManager.for_client(resource)
    return None


register_resource_resolver(_resolve_resource)
