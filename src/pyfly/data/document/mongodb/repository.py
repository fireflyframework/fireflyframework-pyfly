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
"""Generic async repository built on Beanie for MongoDB, running every call in a unit of work.

A :class:`MongoRepository` never holds a session. Every public ``async def`` (on ``MongoRepository``, on a
subclass, and the derived and ``@query`` methods the post-processor compiles) is wrapped so that each call
runs in an **operation scope**, as the relational repository's calls do:

- inside a unit of work for the repository's datasource (``@transactional``, a ``TransactionTemplate``
  block, a listener delivery), the call joins it: every Beanie and pymongo operation gets the unit's
  ``ClientSession`` (``session=``), so the writes are part of the transaction, under the unit's operation
  guard (``asyncio.gather`` fan-out inside a transaction is serialized, never interleaved on one session);
- otherwise the outermost repository call opens a short **auto unit** of its own
  (:class:`~pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager`): a read method
  (``find*``, ``count*``, ``exists*``, ``stream*``, ``get*``, ``scroll*``, by the same naming rule as the
  relational repository) runs in a session without a transaction, retried once when its connection turns
  out to be dead; any other method runs in a transaction that commits at the end of the call, so ``save_all``
  (and a subclass method that writes twice) is atomic. A method that sends one command (``save``, ``delete``,
  ``delete_by_id``, and the bulk and derived deletes when the class has no delete event actions and the ids or
  documents fit one ``$in`` filter, :data:`IN_CHUNK`) runs without one: MongoDB runs a command atomically on
  each document it touches. On a standalone server (no transactions) every auto unit runs without one.

The operation guard is held for one driver command at a time, and never while the document's event actions
run: Beanie's ``@before_event``/``@after_event`` and ``ValidateOnSave`` actions, and so ``BaseDocument``'s audit
hooks and the application's ``AuditorAware``, are user code that may call repositories, and Beanie runs
coroutine actions in ``asyncio.gather`` child tasks. The repository runs them itself around each write, in
Beanie's order, and sends the write with ``skip_actions``. Inside a unit of work their repository calls join
it; outside one they run in auto units of their own (the write of ``save`` or ``delete`` is one command, and an
action's own writes are not part of it).

The datasource is the one named by ``datasource=`` or the class attribute ``__datasource__``; without one it
is the datasource of the manager that serves the client the document class is bound to (``"document"``).

Spring Data semantics, at the fewest round trips:

- ``save`` inserts a new document (its id is made on the client when its type allows: an ``ObjectId``, its
  hex string for a ``str`` id, a ``UUID``) and saves any other as Beanie's ``save`` does (one revision-aware
  ``findAndModify`` upsert); ``save_all`` is one ordered ``bulk_write``
  (``InsertOne`` for the new documents, a revision-aware ``UpdateOne`` for the others) that runs Beanie's
  validation, event actions and state management for each document, as ``save`` does (C039).
- The ids and revisions both make on the client are the documents' only once they are stored: a call that fails
  gives the documents it did not write the id, revision and saved state they had, and so does a rollback of the
  transaction they were written in (after the call returned too), so saving the same objects again retries the
  same writes.
- Ids are converted to the document's id type in every ``_id`` filter (C038): ``MongoRepository[Doc, str]``
  finds an ``ObjectId`` document by its string. An id the type cannot hold matches nothing.
- Sort orders, ``find_all(**filters)`` keys and derived queries name Python fields; ``id`` is stored as
  ``_id`` and an aliased field under its alias (C040). An unknown name raises
  :class:`~pyfly.data.property_resolver.InvalidPropertyError` (a 400), optionally narrowed by the class
  attributes ``__sortable__`` and ``__filterable__``. ``Order.null_handling`` and ``Order.ignore_case`` are
  honored (an aggregation sorts on computed keys when an order needs them), and paging orders by ``_id``
  after the requested orders, so pages are deterministic.
- ``delete_by_id``, ``delete_all_by_id``, ``delete_all`` and derived ``delete_by_*`` are one
  ``delete_one``/``delete_many`` when the document class has no delete event actions, and delete document
  by document (running them) otherwise; the ``*_in_batch`` forms always delete in bulk. ``exists_by_id``
  reads ``{_id: 1}`` of at most one document (C037).
- A persistence failure is raised translated to the kernel's exceptions (``DuplicateKeyException``,
  ``OptimisticLockingFailureException`` for a Beanie revision conflict, ``ConcurrencyException`` for a write
  conflict; :mod:`pyfly.data.document.mongodb.exception_translation`), from the driver's.
- The pending domain events of an aggregate document it saves
  (:class:`~pyfly.data.document.mongodb.document.AggregateDocument`) are published when the unit of work
  commits, by the application's ``DomainEventPublisher``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterable, Sequence, Sized
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Generic, TypeVar, cast, get_args, get_origin, overload

import pymongo
from beanie import Document
from beanie.exceptions import RevisionIdWasChanged
from beanie.odm.actions import ActionDirections, ActionRegistry, EventTypes
from beanie.odm.utils.dump import get_dict, get_top_level_nones
from beanie.odm.utils.parsing import parse_obj
from pymongo import InsertOne, UpdateOne
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import BulkWriteError

from pyfly.container.types import NoAutowire
from pyfly.data.document.mongodb import exception_translation as _translation  # noqa: F401 — registers it
from pyfly.data.document.mongodb.properties import (
    ID_FIELD,
    InvalidIdError,
    coerce_id,
    encode,
    field_path,
    new_id,
)
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager, in_transaction
from pyfly.data.exception_translation import translate_exception
from pyfly.data.page import Page, Slice
from pyfly.data.pageable import NullHandling, Order, Pageable, Sort
from pyfly.data.property_resolver import PropertyResolver
from pyfly.data.transaction.context import bind_state, current_state, reset_state
from pyfly.data.transaction.decorator import is_transactional
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.registry import TransactionManagerRegistry, installed_registry
from pyfly.data.transaction.synchronization import CompletionStatus, TransactionSynchronizationAdapter
from pyfly.data.transaction.template import AutoUnit, complete_auto_unit
from pyfly.data.transaction.unit_of_work import UnitOfWork, cancel_requests

if TYPE_CHECKING:
    from pyfly.data.document.mongodb.specification import MongoSpecification

T = TypeVar("T")
ID = TypeVar("ID")

_logger = logging.getLogger(__name__)

READ_PREFIXES: tuple[str, ...] = ("find", "count", "exists", "stream", "get", "scroll")
"""A repository method whose name starts with one of these runs without a transaction outside one, unless
:data:`WRITE_WORDS` says it writes (the relational repository's rule)."""

WRITE_WORDS: frozenset[str] = frozenset(
    {
        "create",
        "save",
        "insert",
        "update",
        "upsert",
        "delete",
        "remove",
        "merge",
        "persist",
        "store",
        "lock",
        "modify",
        "replace",
        "increment",
        "decrement",
        "set",
        "claim",
    }
)
"""Verbs that make a read-prefixed name a write when one follows an ``and`` or an ``or`` before its criteria
(``find_or_create``, ``find_and_modify``, ``find_one_and_replace``)."""

_CONJUNCTIONS = frozenset({"and", "or"})

Single = bool | Callable[[Any, tuple[Any, ...], dict[str, Any]], bool]
"""Whether a write method sends one command (and so needs no transaction outside one), or a callable that
tells it from the repository and the call's positional and keyword arguments."""

IN_CHUNK = 10_000
"""The most ids one ``$in`` filter carries (a filter document is limited to 16 MB)."""

_SORT_KEY = "__pyfly_sort_{}"
_NULL_KEY = "__pyfly_null_{}"


def is_read_method(name: str) -> bool:
    """Whether a repository method called *name* reads: a read prefix, and no write verb right after an
    ``and``/``or`` before its criteria (:data:`WRITE_WORDS`)."""
    if not name.startswith(READ_PREFIXES):
        return False
    words = name.split("_by_", 1)[0].lower().split("_")
    return not any(
        word in _CONJUNCTIONS and following in WRITE_WORDS for word, following in zip(words, words[1:], strict=False)
    )


def repository_operation(
    function: Callable[..., Coroutine[Any, Any, Any]], *, read: bool, atomic: bool = False, single: Single = False
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Wrap a repository coroutine method so every call runs in an operation scope (module documentation).

    *read* selects an auto unit without a transaction outside a transaction, *single* a write auto unit without
    one: the method sends one command, which MongoDB runs atomically on each document it touches (a callable
    decides from the repository and the call's arguments). *atomic* holds the unit's operation guard for the
    whole call: the framework's own methods that run no user code (a method that runs the document's event
    actions takes the guard for each driver command only, see the module documentation). A method decorated
    with ``@transactional`` opens no auto unit: its own boundary provides the unit. A persistence exception
    leaves the call translated to the kernel's, raised from the driver's, once the unit of work has seen the
    original.
    """
    transactional = is_transactional(function)

    @functools.wraps(function)
    async def operation(self: MongoRepository[Any, Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._pyfly_run(
                function, args, kwargs, read=read, atomic=atomic, single=single, transactional=transactional
            )
        except Exception as error:
            translated = translate_exception(error)
            if translated is error:
                raise
            raise translated from error

    operation.__pyfly_repository_operation__ = True  # type: ignore[attr-defined]
    return operation


def repository_stream(function: Callable[..., AsyncGenerator[Any, None]]) -> Callable[..., AsyncIterator[Any]]:
    """Wrap a repository async-generator method (``stream_all``): it runs on the unit bound when it starts, or
    in a read unit of its own that it keeps until the iterator is exhausted or ``aclose()``d."""

    @functools.wraps(function)
    def stream(self: MongoRepository[Any, Any], *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return self._pyfly_stream(function, args, kwargs)

    stream.__pyfly_repository_operation__ = True  # type: ignore[attr-defined]
    return stream


def _wrap_operations(cls: type, *, atomic: bool) -> None:
    for name, attribute in list(vars(cls).items()):
        if name.startswith("_") or getattr(attribute, "__pyfly_repository_operation__", False):
            continue
        if inspect.isasyncgenfunction(attribute):
            setattr(cls, name, repository_stream(attribute))
        elif inspect.iscoroutinefunction(attribute):
            single = _SINGLE_COMMAND.get(name, False) if atomic else False
            guarded = atomic and name not in _RUNS_EVENT_ACTIONS
            setattr(
                cls, name, repository_operation(attribute, read=is_read_method(name), atomic=guarded, single=single)
            )


def _type_arguments(cls: type) -> tuple[Any, Any] | None:
    """The ``(document, id)`` arguments *cls* binds ``MongoRepository[T, ID]`` to through its generic bases, type
    variables substituted along the way; ``None`` when no base reaches ``MongoRepository``."""

    def walk(klass: type, substitution: dict[Any, Any]) -> tuple[Any, ...] | None:
        for base in klass.__dict__.get("__orig_bases__", klass.__bases__):
            origin = get_origin(base) or base
            if not (isinstance(origin, type) and issubclass(origin, MongoRepository)):
                continue
            arguments = tuple(substitution.get(arg, arg) for arg in get_args(base))
            if origin is MongoRepository:
                return arguments
            found = walk(origin, dict(zip(getattr(origin, "__parameters__", ()), arguments, strict=False)))
            if found is not None:
                return found
        return None

    found = walk(cls, {})
    if found is None:
        return None
    return (found[0] if found else None), (found[1] if len(found) > 1 else None)


def _persistable_hook(cls: type) -> Callable[[Any], bool] | None:
    """The document class's ``is_new`` hook (a method or a property it defines)."""
    static = inspect.getattr_static(cls, "is_new", None)
    if inspect.isfunction(static):
        return lambda entity: bool(entity.is_new())
    if isinstance(static, property):
        return lambda entity: bool(entity.is_new)
    return None


def _chunks(values: Sequence[Any], size: int = IN_CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


_SKIP_ACTIONS: list[ActionDirections | str] = [ActionDirections.BEFORE, ActionDirections.AFTER]
"""What a Beanie write sent under the operation guard skips: the repository runs the event actions itself,
outside the guard (module documentation)."""


async def _run_actions(entity: Any, event: EventTypes, direction: ActionDirections) -> None:
    """Run *entity*'s event actions for *event* and *direction* as Beanie's ``ActionRegistry.run_actions`` does
    (synchronous ones in order, then the coroutines together), awaiting a lone coroutine action in place: a
    task per document and action would cost event-loop turns across a large ``save_all``."""
    actions = ActionRegistry.get_action_list(type(entity), event, direction)
    if not actions:
        return
    coroutines: list[Coroutine[Any, Any, Any]] = []
    for action in actions:
        if inspect.iscoroutinefunction(action):
            coroutines.append(action(entity))
        elif inspect.isfunction(action):
            action(entity)
    if len(coroutines) == 1:
        await coroutines[0]
    elif coroutines:
        await asyncio.gather(*coroutines)


async def _validate(entity: Any) -> None:
    """Beanie's ``validate_self`` of *entity* (its ``validate_on_save`` validation and ``ValidateOnSave``
    actions), skipped when there is nothing to run: validation off, no actions, and no override of it."""
    cls = type(entity)
    if (
        cls.get_settings().validate_on_save
        or getattr(cls, "validate_self", None) is not Document.validate_self
        or any(
            ActionRegistry.get_action_list(cls, EventTypes.VALIDATE_ON_SAVE, direction)
            for direction in (ActionDirections.BEFORE, ActionDirections.AFTER)
        )
    ):
        await entity.validate_self()


def _has_delete_actions(model: type) -> bool:
    return any(
        ActionRegistry.get_action_list(model, EventTypes.DELETE, direction)
        for direction in (ActionDirections.BEFORE, ActionDirections.AFTER)
    )


def deletes_in_one_command(repository: Any, *_call: Any) -> bool:
    """Whether a delete of *repository*'s documents is one command: its class has no delete event actions (a
    derived ``delete_by_*``, whatever it matches; the call's arguments, when given, are not needed)."""
    return not _has_delete_actions(repository._model)


def _bulk_delete_rule(argument: str, *, actions: bool) -> Callable[[Any, tuple[Any, ...], dict[str, Any]], bool]:
    """The ``single`` rule of a bulk delete of the ids or documents its first argument (*argument*) names: one
    command when it deletes everything (``None``) or at most :data:`IN_CHUNK` of them (one ``$in`` filter) and,
    with *actions*, when the class has no delete event actions. Values the call cannot count before it runs (an
    iterator) take a transaction."""

    def one_command(repository: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if actions and _has_delete_actions(repository._model):
            return False
        values = args[0] if args else kwargs.get(argument)
        return values is None or (isinstance(values, Sized) and len(values) <= IN_CHUNK)

    return one_command


_SINGLE_COMMAND: dict[str, Single] = {
    "save": True,
    "delete": True,
    "delete_by_id": True,
    "delete_all_in_batch": _bulk_delete_rule("entities", actions=False),
    "delete_all_by_id_in_batch": _bulk_delete_rule("ids", actions=False),
    "delete_all": _bulk_delete_rule("entities", actions=True),
    "delete_all_by_id": _bulk_delete_rule("ids", actions=True),
}
"""The framework's write methods that send one command: they need no transaction outside one (a subclass's
override of one is not assumed to)."""

_RUNS_EVENT_ACTIONS: frozenset[str] = frozenset(
    {"save", "save_all", "delete", "delete_by_id", "delete_all", "delete_all_by_id"}
)
"""The framework's methods that run the document's event actions: user code, so they hold the operation guard
for each driver command only, never for the whole call (module documentation)."""

_WRITTEN_DOCUMENTS = "pyfly_mongo_written_documents"
"""``UnitOfWork.attributes`` key: the :class:`_RestoreOnRollback` of a unit that runs a transaction."""


class _DocumentState:
    """What a save changes on a document that must match the server's copy: its id, its revision, and its saved
    state (Beanie's state management)."""

    __slots__ = ("document", "id", "previous_saved_state", "revision_id", "saved_state")

    def __init__(self, document: Any) -> None:
        self.document = document
        self.id = document.id
        self.revision_id = document.revision_id
        self.saved_state = document._saved_state
        self.previous_saved_state = document._previous_saved_state

    def restore(self) -> None:
        """Give the document back the state it had when this snapshot was taken."""
        document = self.document
        document.id = self.id
        document.revision_id = self.revision_id
        document._saved_state = self.saved_state
        document._previous_saved_state = self.previous_saved_state


class _RestoreOnRollback(TransactionSynchronizationAdapter):
    """Gives the documents a transaction's saves wrote the state they had before the first of them, when the
    transaction rolls back: none of those writes is stored. A commit whose outcome is unknown leaves them as the
    saves left them."""

    def __init__(self) -> None:
        self.states: dict[int, _DocumentState] = {}

    def remember(self, states: Iterable[_DocumentState]) -> None:
        for state in states:
            self.states.setdefault(id(state.document), state)

    async def after_completion(self, status: CompletionStatus) -> None:
        if status is CompletionStatus.ROLLED_BACK:
            for state in self.states.values():
                state.restore()


def _snapshots(unit: UnitOfWork, documents: Iterable[Any]) -> list[_DocumentState]:
    """The state of *documents* before a save writes them; in a transaction, also kept so that a rollback of *unit*
    gives it back."""
    states = [_DocumentState(document) for document in documents]
    if in_transaction(unit):
        restorer = unit.attributes.get(_WRITTEN_DOCUMENTS)
        if restorer is None:
            restorer = _RestoreOnRollback()
            unit.register_synchronization(restorer)
            unit.attributes[_WRITTEN_DOCUMENTS] = restorer
        restorer.remember(states)
    return states


def _written_before(error: BaseException, count: int) -> int:
    """How many of the *count* operations an ordered bulk write that raised *error* outside a transaction wrote:
    the ones before its first write error (all of them when only the write concern failed), none for any other
    error (nothing written, or an outcome the driver cannot know, as Beanie's ``insert`` takes it)."""
    if not isinstance(error, BulkWriteError):
        return 0
    errors = error.details.get("writeErrors") or []
    return min(int(failure["index"]) for failure in errors) if errors else count


class MongoRepository(Generic[T, ID]):
    """Generic CRUD repository for Beanie documents (see the module documentation).

    Implements the Spring-parity ``PagingAndSortingRepository`` contract and the batch deletes and slices of
    :class:`~pyfly.data.ports.outbound.BatchRepository` on MongoDB.

    Type Parameters:
        T: The document type (a Beanie ``Document`` subclass).
        ID: The id type callers pass (``str`` for an ``ObjectId`` document works: ids are converted).

    Usage::

        class UserDocumentRepository(MongoRepository[UserDocument, str]):
            pass  # document type extracted from the declaration

        class AuditRepository(MongoRepository[AuditEntry, str]):
            __datasource__ = "audit"          # a second document datasource, registered by the application
            __sortable__ = ("at", "actor")    # the only fields a Sort may name
    """

    _entity_type: type | None = None
    _id_type: type | None = None
    __datasource__: ClassVar[str | None] = None
    """The datasource a subclass's calls run on (``None``: the manager of the document's client)."""
    __sortable__: ClassVar[Sequence[str] | None] = None
    """The only fields a ``Sort`` may name (``None``: every field of the document)."""
    __filterable__: ClassVar[Sequence[str] | None] = None
    """The only fields ``find_all(**filters)`` may filter on (``None``: every field of the document)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        arguments = _type_arguments(cls)
        if arguments is not None:
            entity, identifier = arguments
            if entity is not None and not isinstance(entity, TypeVar):
                cls._entity_type = entity
            if identifier is not None and not isinstance(identifier, TypeVar):
                cls._id_type = identifier
        _wrap_operations(cls, atomic=bool(vars(cls).get("_pyfly_framework_repository", False)))

    def __init__(
        self,
        model: type[T] | None = None,
        *,
        datasource: Annotated[str | None, NoAutowire] = None,
    ) -> None:
        resolved = model or getattr(type(self), "_entity_type", None)
        if resolved is None:
            raise TypeError(
                f"{type(self).__name__} requires either MongoRepository[Document, ID] "
                f"declaration or explicit model argument"
            )
        self._model: type[T] = cast(type[T], resolved)
        self._datasource: str | None = datasource or type(self).__datasource__
        self._transaction_managers: TransactionManagerRegistry | None = None
        self._sort_names: PropertyResolver | None = None
        self._filter_names: PropertyResolver | None = None
        self._field_names: PropertyResolver | None = None
        self._is_new_hook = _persistable_hook(self._model)
        # An allow-list with a typo fails here, when the context builds the repository, not on the first read.
        if type(self).__sortable__ is not None:
            self._sort_resolver()
        if type(self).__filterable__ is not None:
            self._filter_resolver()

    # ------------------------------------------------------------------
    # Units of work
    # ------------------------------------------------------------------

    @property
    def datasource(self) -> str:
        """The datasource this repository's calls run on."""
        return self._transaction_manager().datasource

    def _bind_transaction_managers(self, managers: TransactionManagerRegistry) -> None:
        """Resolve this repository's transaction manager from *managers* (the application context's)."""
        self._transaction_managers = managers

    def _collection(self) -> Any:
        return self._model.get_pymongo_collection()  # type: ignore[attr-defined]

    def _transaction_manager(self) -> MongoTransactionManager:
        """The manager of this repository's datasource: the one ``datasource=`` names, else the manager of the
        client the document class is bound to (the application's, or an ad-hoc one)."""
        client = self._collection().database.client
        registry = self._transaction_managers or installed_registry()
        if self._datasource is not None:
            if registry is None:
                raise IllegalTransactionStateError(
                    f"{type(self).__name__} runs on datasource '{self._datasource}', and no application context "
                    "is running to name its transaction manager.",
                    datasource=self._datasource,
                )
            named = registry.get(self._datasource)
            if not isinstance(named, MongoTransactionManager) or not named.owns(client):
                raise IllegalTransactionStateError(
                    f"{type(self).__name__} runs on datasource '{self._datasource}', whose transaction manager "
                    f"({named!r}) does not serve the MongoDB client {self._model.__name__} is bound to.",
                    datasource=self._datasource,
                )
            return named
        if registry is not None:
            found = registry.find_by_resource(client)
            if isinstance(found, MongoTransactionManager):
                return found
        return MongoTransactionManager.for_client(client)

    def _current_unit(self) -> UnitOfWork:
        """The unit of the current call (its operation scope, else the bound unit of the datasource)."""
        datasource = self._transaction_manager().datasource
        state = current_state()
        unit = state.scope(datasource) or state.unit(datasource)
        if unit is None:
            raise IllegalTransactionStateError(
                f"{type(self).__name__} has no session here: it runs each call in a unit of work, and none is "
                f"active for datasource '{datasource}'. Use the session inside a repository method (it opens a "
                "unit of its own) or inside @transactional.",
                datasource=datasource,
            )
        unit.check_usable()
        return unit

    @property
    def _session(self) -> AsyncClientSession:
        """The ``ClientSession`` of the current call's unit (for a custom method that calls Beanie itself)."""
        return cast(AsyncClientSession, self._current_unit().resource)

    def _query(self, **filters: Any) -> Any:
        """A Beanie find query of the documents whose fields equal *filters*, on the current call's session (the
        helper of earlier releases, kept for subclasses written against it; ``find_all(**filters)`` is the
        public form). The names are validated and mapped as ``find_all``'s; the query's terminal call does not
        take the operation guard, as any Beanie call a custom method makes with :attr:`_session`."""
        filter_document = self._filters(filters)
        return self._model.find(filter_document, session=self._session)  # type: ignore[attr-defined]

    def _writable_unit(self) -> UnitOfWork:
        """The unit of the current call, refused when it is read-only: a write checks it before it runs the
        document's validation and event actions (user code)."""
        unit = self._current_unit()
        if unit.read_only:
            raise IllegalTransactionStateError(
                f"{unit.describe()} is read-only and {type(self).__name__} cannot write in it. "
                + (
                    "A repository read method (find*, count*, exists*, stream*, get*) runs in a read-only unit; "
                    "give a method that writes another name, or call it inside @transactional."
                    if unit.auto
                    else "Drop read_only=True from the boundary, or write in a unit of its own "
                    "(Propagation.REQUIRES_NEW)."
                ),
                datasource=unit.datasource,
            )
        return unit

    @contextlib.asynccontextmanager
    async def _operation(self, *, write: bool = False) -> AsyncIterator[AsyncClientSession]:
        """One operation on the current unit's session, under its guard (a failure is recorded on the unit)."""
        unit = self._writable_unit() if write else self._current_unit()
        async with unit.operation():
            yield cast(AsyncClientSession, unit.resource)

    async def _pyfly_run(
        self,
        function: Callable[..., Coroutine[Any, Any, Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        read: bool,
        atomic: bool,
        single: Single = False,
        transactional: bool = False,
    ) -> Any:
        manager = self._transaction_manager()
        datasource = manager.datasource
        state = current_state()
        scoped = state.scope(datasource)
        if scoped is not None:
            return await _run_on(scoped, function, self, args, kwargs, atomic)
        unit = state.unit(datasource)
        if unit is not None:
            unit.check_usable()
            token = bind_state(state.with_scope(datasource, unit))
            try:
                return await _run_on(unit, function, self, args, kwargs, atomic)
            finally:
                reset_state(token)
        if transactional:
            return await function(self, *args, **kwargs)
        one_command = single(self, args, kwargs) if callable(single) else single
        attempts = 2 if read else 1
        for attempt in range(1, attempts + 1):
            try:
                async with AutoUnit(manager, read_only=read, autocommit=True if one_command else None) as auto:
                    return await _run_on(auto, function, self, args, kwargs, atomic)
            except Exception as error:
                if attempt < attempts and manager.is_disconnect(error):
                    _logger.info(
                        "repository_read_retried_after_disconnect",
                        extra={"repository": type(self).__name__, "datasource": datasource},
                    )
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover — the loop returns or raises

    async def _pyfly_stream(
        self, function: Callable[..., AsyncGenerator[Any, None]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        """The stream of :func:`repository_stream`: its steps run with the unit bound, never across a yield."""
        try:
            manager = self._transaction_manager()
            datasource = manager.datasource
            state = current_state()
            unit = state.scope(datasource) or state.unit(datasource)
            owned = unit is None
            since = cancel_requests()
            if unit is None:
                unit = await manager.open_auto_unit(read_only=True)
            else:
                unit.check_usable()
            inner = function(self, *args, **kwargs)
            step = inner.__anext__
            scoped = current_state().with_scope(datasource, unit)
            error: BaseException | None = None
            try:
                while True:
                    unit.check_usable()
                    token = bind_state(scoped)
                    try:
                        item = await step()
                    except StopAsyncIteration:
                        break
                    finally:
                        reset_state(token)
                    yield item
            except BaseException as raised:
                error = raised
                raise
            finally:
                token = bind_state(scoped)
                try:
                    await inner.aclose()
                finally:
                    reset_state(token)
                if owned:
                    await complete_auto_unit(unit, error, since=since)
        except Exception as failure:
            translated = translate_exception(failure)
            if translated is failure:
                raise
            raise translated from failure

    # ------------------------------------------------------------------
    # Names, ids, sorts
    # ------------------------------------------------------------------

    def _sort_resolver(self) -> PropertyResolver:
        if self._sort_names is None:
            self._sort_names = PropertyResolver.for_entity(self._model, allowed=type(self).__sortable__)
        return self._sort_names

    def _filter_resolver(self) -> PropertyResolver:
        if self._filter_names is None:
            self._filter_names = PropertyResolver.for_entity(self._model, allowed=type(self).__filterable__)
        return self._filter_names

    def _filters(self, filters: dict[str, Any]) -> dict[str, Any]:
        """``find_all(**filters)`` as a filter document: each name validated and stored-name mapped, an id
        converted to the document's id type (one it cannot hold matches nothing)."""
        document: dict[str, Any] = {}
        for name, value in filters.items():
            path = field_path(self._model, name, usage="filter", resolver=self._filter_resolver())
            if path == ID_FIELD:
                try:
                    value = coerce_id(self._model, value)
                except InvalidIdError:
                    return {ID_FIELD: {"$in": []}}
            document[path] = value
        return document

    def _ids(self, ids: Iterable[Any]) -> list[Any]:
        """*ids* converted to the document's id type, those it cannot hold left out (they match nothing)."""
        converted: list[Any] = []
        for value in ids:
            try:
                converted.append(coerce_id(self._model, value))
            except InvalidIdError:
                continue
        return converted

    def _criteria(self, filter_document: dict[str, Any]) -> dict[str, Any]:
        """*filter_document* as the driver gets it: encoded with the document's BSON encoders, with the class
        filter of an inheritance hierarchy (what Beanie's own queries add)."""
        return cast(dict[str, Any], self._model.find(filter_document).get_filter_query())  # type: ignore[attr-defined]

    def _order_path(self, order: Order, *, trusted: bool = False) -> str:
        """The stored name of *order*'s property: any field of the document for an order the repository's own
        code declares (*trusted*: a derived query's ``order_by``), the ``__sortable__`` fields for any other."""
        if trusted:
            if self._field_names is None:
                self._field_names = PropertyResolver.for_entity(self._model)
            resolver = self._field_names
        else:
            resolver = self._sort_resolver()
        return field_path(self._model, order.property, usage="sort", resolver=resolver)

    def _sort_plan(
        self, sort: Sort | None, *, tiebreak: bool = False, trusted: int = 0
    ) -> tuple[list[tuple[str, int]], dict[str, Any]]:
        """The sort specification of *sort* and the computed keys it needs (``$addFields``): a key per order that
        ignores case, and a NULL key per order whose NULL placement is not MongoDB's own (first ascending, last
        descending). With *tiebreak*, ``_id`` ends the orders (deterministic pages). The first *trusted* orders
        are the repository's own (a derived query's ``order_by``): ``__sortable__`` does not narrow them."""
        spec: list[tuple[str, int]] = []
        computed: dict[str, Any] = {}
        for index, order in enumerate(sort.orders if sort is not None else ()):
            path = self._order_path(order, trusted=index < trusted)
            direction = pymongo.ASCENDING if order.direction == "asc" else pymongo.DESCENDING
            nulls = order.null_handling
            native_first = order.direction == "asc"
            if (nulls is NullHandling.NULLS_LAST and native_first) or (
                nulls is NullHandling.NULLS_FIRST and not native_first
            ):
                key = _NULL_KEY.format(index)
                is_null = {"$in": [{"$type": f"${path}"}, ["null", "missing"]]}
                computed[key] = {"$cond": [is_null, 1, 0]}
                spec.append((key, pymongo.ASCENDING if nulls is NullHandling.NULLS_LAST else pymongo.DESCENDING))
            if order.ignore_case:
                key = _SORT_KEY.format(index)
                value = f"${path}"
                computed[key] = {"$cond": [{"$eq": [{"$type": value}, "string"]}, {"$toLower": value}, value]}
                spec.append((key, direction))
            else:
                spec.append((path, direction))
        if tiebreak and not any(path == ID_FIELD for path, _direction in spec):
            spec.append((ID_FIELD, pymongo.ASCENDING))
        return spec, computed

    async def _find(
        self,
        filter_document: dict[str, Any],
        *,
        sort: Sort | None = None,
        skip: int | None = None,
        limit: int | None = None,
        tiebreak: bool = False,
        trusted: int = 0,
    ) -> list[T]:
        """The documents matching *filter_document*, sorted and cut (one ``find``, or one ``aggregate`` when
        an order needs computed keys)."""
        spec, computed = self._sort_plan(sort, tiebreak=tiebreak, trusted=trusted)
        model = self._model
        if computed:
            pipeline: list[dict[str, Any]] = [
                {"$match": self._criteria(filter_document)},
                {"$addFields": computed},
                {"$sort": dict(spec)},
            ]
            if skip:
                pipeline.append({"$skip": skip})
            if limit is not None:
                pipeline.append({"$limit": limit})
            pipeline.append({"$unset": list(computed)})
            async with self._operation() as session:
                cursor = await self._collection().aggregate(pipeline, session=session)
                rows = await cursor.to_list()
            return [cast(T, parse_obj(model, row)) for row in rows]  # type: ignore[arg-type]
        async with self._operation() as session:
            query = model.find(filter_document, session=session)  # type: ignore[attr-defined]
            if spec:
                query = query.sort(spec)
            if skip:
                query = query.skip(skip)
            if limit is not None:
                query = query.limit(limit)
            return cast(list[T], await query.to_list())

    async def _count(self, filter_document: dict[str, Any]) -> int:
        async with self._operation() as session:
            return int(await self._collection().count_documents(self._criteria(filter_document), session=session))

    async def _exists(self, filter_document: dict[str, Any]) -> bool:
        async with self._operation() as session:
            found = await self._collection().find_one(self._criteria(filter_document), {ID_FIELD: 1}, session=session)
        return found is not None

    async def _page(self, filter_document: dict[str, Any], pageable: Pageable, *, trusted: int = 0) -> Page[T]:
        if not pageable.is_paged:
            items = await self._find(filter_document, sort=pageable.sort, trusted=trusted)
            return Page(items=items, total=len(items), page=pageable.page, size=len(items) or 1)
        items = await self._find(
            filter_document,
            sort=pageable.sort,
            skip=pageable.offset,
            limit=pageable.size,
            tiebreak=True,
            trusted=trusted,
        )
        total = _total_from_content(pageable, len(items))
        if total is None:
            total = await self._count(filter_document)
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    async def _slice(self, filter_document: dict[str, Any], pageable: Pageable, *, trusted: int = 0) -> Slice[T]:
        if not pageable.is_paged:
            items = await self._find(filter_document, sort=pageable.sort, trusted=trusted)
            return Slice(items=items, page=pageable.page, size=len(items) or 1, has_next=False)
        rows = await self._find(
            filter_document,
            sort=pageable.sort,
            skip=pageable.offset,
            limit=pageable.size + 1,
            tiebreak=True,
            trusted=trusted,
        )
        has_next = len(rows) > pageable.size
        return Slice(items=rows[: pageable.size], page=pageable.page, size=pageable.size, has_next=has_next)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _is_new(self, entity: Any) -> bool:
        if self._is_new_hook is not None:
            return self._is_new_hook(entity)
        return entity.id is None

    def _assign_new_id(self, entity: Any) -> None:
        if entity.id is None:
            generated = new_id(self._model)
            if generated is not None:
                entity.id = generated

    def _collect_events(self, entities: Iterable[Any]) -> None:
        """Hand the pending domain events of the aggregates just written to the application's publisher, to
        publish when the call's unit of work commits."""
        pending = [
            entity
            for entity in entities
            if callable(getattr(entity, "pending_events", None)) and callable(getattr(entity, "clear_events", None))
        ]
        pending = [entity for entity in pending if entity.pending_events()]
        if not pending:
            return
        try:
            from pyfly.eda.domain_events import active_domain_event_publisher
        except ImportError:  # pragma: no cover — the EDA module is part of the core distribution
            return
        publisher = active_domain_event_publisher()
        if publisher is None:
            return
        unit = self._current_unit()
        for entity in pending:
            publisher.collect(entity, unit)

    async def save(self, entity: T) -> T:
        """Insert a new document, or save one that is not new (``is_new()`` hook, else ``id is None``) as Beanie's
        ``save`` does: validation and event actions in Beanie's order, around one command (``insert``, or the
        revision-aware ``findAndModify`` upsert of a stored document).

        A save whose write fails, or whose transaction rolls back, gives the document back the id, revision and
        saved state it had, so saving it again retries the same write (a new document is inserted again)."""
        document: Any = entity
        unit = self._writable_unit()
        settings = self._model.get_settings()  # type: ignore[attr-defined]
        new = self._is_new(document)
        (state,) = _snapshots(unit, (document,))
        try:
            await _validate(document)
            if new:
                fields = await self._before_insert(document, settings)
                async with self._operation(write=True) as session:
                    result = await self._collection().insert_one(fields, session=session)
            else:
                await _run_actions(document, EventTypes.SAVE, ActionDirections.BEFORE)
                await _run_actions(document, EventTypes.UPDATE, ActionDirections.BEFORE)
                changes: list[dict[str, Any]] = [
                    {"$set": get_dict(document, to_db=True, keep_nulls=settings.keep_nulls)}
                ]
                if not settings.keep_nulls:
                    nones = get_top_level_nones(document)
                    if nones:
                        changes.append({"$unset": dict.fromkeys(nones, "")})
                async with self._operation(write=True) as session:
                    # Beanie's update of a stored document (its revision check and state), without its actions.
                    await document.update(*changes, session=session, upsert=True, skip_actions=_SKIP_ACTIONS)
        except BaseException:
            state.restore()
            raise
        if new:
            if document.id is None:
                document.id = _as_id(self._model, result.inserted_id)
            document._save_state()
            await _run_actions(document, EventTypes.INSERT, ActionDirections.AFTER)
        else:
            await _run_actions(document, EventTypes.UPDATE, ActionDirections.AFTER)
            await _run_actions(document, EventTypes.SAVE, ActionDirections.AFTER)
        self._collect_events((document,))
        return entity

    def _stored(self, entity: Any, new: bool, document: dict[str, Any]) -> None:
        """Record that ``save_all`` wrote *entity* (from its operation's *document*): the id the driver gave a new
        one, and its saved state."""
        if new and entity.id is None and ID_FIELD in document:
            entity.id = _as_id(self._model, document[ID_FIELD])
        entity._save_state()

    async def _before_insert(self, entity: Any, settings: Any) -> dict[str, Any]:
        """Ready a new document for its insert: its id made on the client, its insert actions run, a revision
        given; returns the fields to insert."""
        self._assign_new_id(entity)
        await _run_actions(entity, EventTypes.INSERT, ActionDirections.BEFORE)
        if settings.use_revision:
            entity.revision_id = uuid.uuid4()
        return cast(dict[str, Any], get_dict(entity, to_db=True, keep_nulls=settings.keep_nulls))

    async def save_all(self, entities: Iterable[T]) -> list[T]:
        """Persist *entities* with one ordered ``bulk_write`` (module documentation): validation, event actions,
        revision checks and state management as ``save`` runs them, in the call's unit of work.

        Outside a transaction on a replica set the call is one transaction, which MongoDB bounds in time
        (``transactionLifetimeLimitSeconds``, 60 s by default) and size (``TransactionTooLargeForCache``): save a
        large data set in batches of a few thousand documents, each atomic on its own.

        A call that fails gives the documents it did not write the id, revision and saved state they had, and so
        does a rollback of its transaction, so saving the same documents again retries the same writes. Without a
        transaction (a standalone server) the documents the bulk write reached before a failing one are stored,
        and keep their new ids and revisions."""
        items: list[Any] = list(entities)
        if not items:
            return []
        settings = self._model.get_settings()  # type: ignore[attr-defined]
        unit = self._writable_unit()
        transactional = in_transaction(unit)
        states = _snapshots(unit, items)
        operations: list[InsertOne[Any] | UpdateOne] = []
        plans: list[tuple[Any, bool, dict[str, Any]]] = []
        guarded = 0
        conflict: RevisionIdWasChanged | None = None
        try:
            for entity in items:
                new = self._is_new(entity)
                await _validate(entity)
                if new:
                    document = await self._before_insert(entity, settings)
                    operations.append(InsertOne(document))
                else:
                    await _run_actions(entity, EventTypes.SAVE, ActionDirections.BEFORE)
                    await _run_actions(entity, EventTypes.UPDATE, ActionDirections.BEFORE)
                    criteria: dict[str, Any] = {ID_FIELD: entity.id}
                    upsert = True
                    if settings.use_revision:
                        if entity.revision_id is not None:
                            criteria["revision_id"] = entity.revision_id
                            upsert = False
                            guarded += 1
                        entity.revision_id = uuid.uuid4()
                    document = get_dict(entity, to_db=True, keep_nulls=settings.keep_nulls)
                    update: dict[str, Any] = {"$set": document}
                    if not settings.keep_nulls:
                        nones = get_top_level_nones(entity)
                        if nones:
                            update["$unset"] = dict.fromkeys(nones, "")
                    operations.append(UpdateOne(encode(self._model, criteria), update, upsert=upsert))
                plans.append((entity, new, document))
            async with self._operation(write=True) as session:
                result = await self._collection().bulk_write(operations, ordered=True, session=session)
                updates = len(items) - result.inserted_count
                if guarded and result.matched_count + result.upserted_count < updates:
                    conflict = RevisionIdWasChanged()
                    if transactional:
                        unit.set_rollback_only(conflict)  # the documents written before it must not commit
                    raise conflict
        except BaseException as error:
            if not transactional and error is conflict:
                raise  # every document was sent: the ones whose revision matched are stored with their new one
            written = 0 if transactional else _written_before(error, len(plans))
            for entity, new, document in plans[:written]:
                self._stored(entity, new, document)
            for state in states[written:]:
                state.restore()
            raise
        for entity, new, document in plans:
            self._stored(entity, new, document)
            if new:
                await _run_actions(entity, EventTypes.INSERT, ActionDirections.AFTER)
            else:
                await _run_actions(entity, EventTypes.UPDATE, ActionDirections.AFTER)
                await _run_actions(entity, EventTypes.SAVE, ActionDirections.AFTER)
        self._collect_events(items)
        return cast(list[T], items)

    async def delete(self, entity: T) -> None:
        """Delete a document instance (its delete event actions run)."""
        await self._delete_document(entity)

    async def _delete_document(self, entity: Any) -> None:
        """Beanie's ``delete`` of *entity*: its delete actions, outside the guard, around one ``delete``."""
        self._writable_unit()
        await _run_actions(entity, EventTypes.DELETE, ActionDirections.BEFORE)
        async with self._operation(write=True) as session:
            await entity.delete(session=session, skip_actions=_SKIP_ACTIONS)
        await _run_actions(entity, EventTypes.DELETE, ActionDirections.AFTER)

    async def delete_by_id(self, id: ID) -> None:
        """Delete the document with this id (``delete_one``; with delete event actions, load and delete it)."""
        keys = self._ids((id,))
        if not keys:
            return
        await self._delete_where({ID_FIELD: keys[0]}, many=False)

    async def delete_all_by_id(self, ids: Iterable[ID]) -> None:
        """Delete the documents with these ids (Spring ``deleteAllById``)."""
        keys = self._ids(ids)
        for chunk in _chunks(keys):
            await self._delete_where({ID_FIELD: {"$in": chunk}}, many=True)

    async def delete_all(self, entities: Iterable[T] | None = None) -> None:
        """Delete the given documents, or ALL when ``entities`` is ``None`` (Spring ``deleteAll``); document by
        document when the class has delete event actions."""
        if entities is None:
            await self._delete_where({}, many=True)
            return
        items: list[Any] = list(entities)
        if not items:
            return
        if _has_delete_actions(self._model):
            for entity in items:
                await self._delete_document(entity)
            return
        for chunk in _chunks([entity.id for entity in items if entity.id is not None]):
            await self._delete_where({ID_FIELD: {"$in": chunk}}, many=True, actions=False)

    async def delete_all_in_batch(self, entities: Iterable[T] | None = None) -> None:
        """Delete in bulk, bypassing the delete event actions (Spring ``deleteAllInBatch``)."""
        if entities is None:
            await self._delete_where({}, many=True, actions=False)
            return
        keys = [entity.id for entity in entities if entity.id is not None]  # type: ignore[attr-defined]
        for chunk in _chunks(keys):
            await self._delete_where({ID_FIELD: {"$in": chunk}}, many=True, actions=False)

    async def delete_all_by_id_in_batch(self, ids: Iterable[ID]) -> None:
        """Delete the documents with these ids in bulk, bypassing the delete event actions."""
        for chunk in _chunks(self._ids(ids)):
            await self._delete_where({ID_FIELD: {"$in": chunk}}, many=True, actions=False)

    async def _delete_where(self, filter_document: dict[str, Any], *, many: bool, actions: bool = True) -> int:
        """Delete what *filter_document* matches: one command, or document by document (loading them) when the
        class has delete event actions and *actions* is true. Returns how many were deleted."""
        self._writable_unit()
        if actions and _has_delete_actions(self._model):
            documents = await self._find(filter_document, limit=None if many else 1)
            for document in documents:
                await self._delete_document(document)
            return len(documents)
        encoded = self._criteria(filter_document)
        async with self._operation(write=True) as session:
            collection = self._collection()
            if many:
                result = await collection.delete_many(encoded, session=session)
            else:
                result = await collection.delete_one(encoded, session=session)
        return int(result.deleted_count)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def find_by_id(self, id: ID) -> T | None:
        """The document with this id, or ``None``."""
        keys = self._ids((id,))
        if not keys:
            return None
        async with self._operation() as session:
            return cast("T | None", await self._model.get(keys[0], session=session))  # type: ignore[attr-defined]

    async def find_all_by_id(self, ids: Iterable[ID]) -> list[T]:
        """The documents with these ids (Spring ``findAllById``)."""
        found: list[T] = []
        for chunk in _chunks(self._ids(ids)):
            found.extend(await self._find({ID_FIELD: {"$in": chunk}}))
        return found

    async def exists_by_id(self, id: ID) -> bool:
        """Whether a document with this id exists: ``{_id: 1}`` of at most one document."""
        keys = self._ids((id,))
        return bool(keys) and await self._exists({ID_FIELD: keys[0]})

    async def count(self) -> int:
        """The number of documents."""
        return await self._count({})

    @overload
    async def find_all(self, criteria: None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, **filters: Any) -> Page[T]: ...
    async def find_all(self, criteria: Sort | Pageable | None = None, **filters: Any) -> list[T] | Page[T]:
        """Spring ``findAll`` family.

        - ``find_all()`` / ``find_all(active=True)`` → ``list[T]`` (optionally filtered by field equality)
        - ``find_all(Sort.by("name"))`` → sorted ``list[T]``
        - ``find_all(Pageable.of(1, 20))`` → ``Page[T]``
        """
        filter_document = self._filters(filters)
        if isinstance(criteria, Pageable):
            return await self._page(filter_document, criteria)
        return await self._find(filter_document, sort=criteria if isinstance(criteria, Sort) else None)

    async def find_slice(self, pageable: Pageable, **filters: Any) -> Slice[T]:
        """A page and whether another follows, with no count (Spring ``Slice``)."""
        return await self._slice(self._filters(filters), pageable)

    async def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]:
        """Stream documents lazily (``Flux<T>`` analogue) through one cursor on the unit's session."""
        spec, computed = self._sort_plan(criteria)
        model = self._model
        filter_document = self._criteria(self._filters(filters))
        unit = self._current_unit()
        collection = self._collection()
        if computed:
            pipeline: list[dict[str, Any]] = [
                {"$match": filter_document},
                {"$addFields": computed},
                {"$sort": dict(spec)},
                {"$unset": list(computed)},
            ]
            async with unit.operation():
                cursor = await collection.aggregate(pipeline, session=unit.resource)
        else:
            cursor = collection.find(filter_document, session=unit.resource, sort=spec or None)
        try:
            while True:
                async with unit.operation():
                    try:
                        row = await cursor.next()
                    except StopAsyncIteration:
                        return
                yield cast(T, parse_obj(model, row))  # type: ignore[arg-type]
        finally:
            if unit.completed:
                await cursor.close()  # its session's unit is over: no other command can run on it any more
            else:
                # killCursors on the unit's session, under its guard like every other command on it.
                async with unit.operation():
                    await cursor.close()

    # ------------------------------------------------------------------
    # Specification extensions (PyFly)
    # ------------------------------------------------------------------

    async def find_all_by_spec(self, spec: MongoSpecification[T]) -> list[T]:
        """The documents matching a specification."""
        return await self._find(spec.to_predicate(self._model, {}))

    async def find_all_by_spec_paged(self, spec: MongoSpecification[T], pageable: Pageable) -> Page[T]:
        """A page of the documents matching a specification."""
        return await self._page(spec.to_predicate(self._model, {}), pageable)

    async def find_slice_by_spec(self, spec: MongoSpecification[T], pageable: Pageable) -> Slice[T]:
        """A slice of the documents matching a specification, with no count."""
        return await self._slice(spec.to_predicate(self._model, {}), pageable)


def _as_id(model: type, value: Any) -> Any:
    """A server-assigned ``_id`` converted to the document's id type."""
    try:
        return coerce_id(model, value)
    except InvalidIdError:
        return value


def _total_from_content(pageable: Pageable, count: int) -> int | None:
    """The total a page's own content proves, or ``None`` when a count is needed (Spring's
    ``PageableExecutionUtils``)."""
    if pageable.offset == 0:
        return count if count < pageable.size else None
    if 0 < count < pageable.size:
        return pageable.offset + count
    return None


async def _run_on(
    unit: UnitOfWork,
    function: Callable[..., Coroutine[Any, Any, Any]],
    repository: MongoRepository[Any, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    atomic: bool,
) -> Any:
    if not atomic:
        unit.check_usable()
        return await function(repository, *args, **kwargs)
    async with unit.guard:
        unit.check_usable()
        return await function(repository, *args, **kwargs)


# The framework's own methods are atomic operations; subclasses are wrapped by __init_subclass__.
_wrap_operations(MongoRepository, atomic=True)
