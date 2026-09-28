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
"""Beanie initialization, and the process-level registry of document bindings and shared clients.

:class:`BeanieInitializer` is the lifecycle bean that binds the application's document classes to its database
(``init_beanie``) when the context starts. It finds them (C035):

- from every ``MongoRepository`` bean class (its document, any Beanie ``Document``, not only ``BaseDocument``);
- from every registered Beanie document class;
- from ``pyfly.data.document.models``: document classes, or modules and packages whose document classes are
  all taken, by dotted name;
- and, recursively, from the ``Link``/``BackLink`` fields of those documents, so a linked document without a
  repository of its own is bound too.

Beanie binds a document class to one database for the whole process (C108): a second application context that
bound the same classes to another client or database would take them away from the first, and stopping it
would close the client the first one still uses. :data:`BINDINGS` keeps one binding per class, reference
counted:

- a context that binds a class to the client and database it is bound to already shares the binding (and the
  client: the document auto-configuration builds one client per distinct configuration, shared by the contexts
  that configure the same);
- a context that would bind it to another client or database fails to start with :class:`DocumentBindingError`
  (one document datasource per process);
- the client is closed when the last context that uses it releases it, as the context disposes its resources,
  after every bean that could still write with it stopped.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import threading
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any

from pyfly.kernel.exceptions import InfrastructureException

if TYPE_CHECKING:
    import pymongo

    from pyfly.container.container import Container
    from pyfly.core.config import Config

logger = logging.getLogger(__name__)

DOCUMENT_SCHEMA_PHASE = -(1 << 20)
"""The lifecycle phase of :class:`BeanieInitializer`: before the application's lifecycle beans (they may write
documents when they start), after the relational migrations."""


class DocumentBindingError(InfrastructureException):
    """A document class is bound to another client or database by a context still running (C108)."""


@dataclass
class _Binding:
    client: Any
    database: str
    users: int = 0


@dataclass
class _SharedClient:
    client: Any
    key: tuple[str, str] | None
    users: int = 0


class DocumentBindings:
    """The document classes bound in this process and the clients they use, reference counted (see the module
    documentation). :data:`BINDINGS` is the one instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bindings: dict[type, _Binding] = {}
        self._clients: dict[int, _SharedClient] = {}
        self._by_settings: dict[tuple[str, str], _SharedClient] = {}

    # -- clients ---------------------------------------------------------------------------------------------

    def client_for(self, uri: str, options: Mapping[str, Any]) -> pymongo.AsyncMongoClient[Any]:
        """The client of *uri* with *options*: the one a running context already uses for the same settings,
        or a new one (which replaces a client built for them that no context started with). Its users are counted
        by :meth:`bind` and :meth:`release`."""
        from pymongo import AsyncMongoClient

        # The listeners are the context's own adapters (metrics): they do not make a different client.
        settings = sorted((name, repr(value)) for name, value in options.items() if name != "event_listeners")
        key = (uri, repr(settings))
        with self._lock:
            shared = self._by_settings.get(key)
            if shared is not None and shared.users > 0:
                return shared.client  # type: ignore[no-any-return]
            if shared is not None and self._clients.get(id(shared.client)) is shared:
                del self._clients[id(shared.client)]
            client: AsyncMongoClient[Any] = AsyncMongoClient(uri, **dict(options))
            shared = _SharedClient(client, key)
            self._by_settings[key] = shared
            self._clients[id(client)] = shared
            return client

    # -- bindings --------------------------------------------------------------------------------------------

    def bind(self, client: Any, database: str, models: Iterable[type]) -> list[type]:
        """Record that a context binds *models* to *database* on *client*, and count it as a user of the client.

        Returns the classes not bound to that database on that client yet (``init_beanie`` binds them). Raises
        :class:`DocumentBindingError`, recording nothing, when a class is bound to another client or database
        by a context still running.
        """
        models = list(dict.fromkeys(models))
        with self._lock:
            for model in models:
                bound = self._bindings.get(model)
                if bound is not None and bound.users > 0 and (bound.client is not client or bound.database != database):
                    raise DocumentBindingError(
                        f"{model.__name__} is bound to database {bound.database!r} of another MongoDB client by an "
                        f"application context that is still running, and Beanie binds a document class to one "
                        f"database per process: this context cannot bind it to {database!r}. Run one document "
                        "datasource per process: stop the other context first, or configure both with the same "
                        "pyfly.data.document.uri, database and client options (they then share the client).",
                        code="DOCUMENT_BINDING_CONFLICT",
                        context={"document": model.__name__, "database": database, "bound_to": bound.database},
                    )
            unbound = [
                model
                for model in models
                if (bound := self._bindings.get(model)) is None
                or bound.users == 0
                or bound.client is not client
                or bound.database != database
            ]
            for model in models:
                bound = self._bindings.get(model)
                if bound is None or bound.users == 0:
                    bound = self._bindings[model] = _Binding(client, database)
                bound.users += 1
            shared = self._clients.get(id(client))
            if shared is None or shared.client is not client:
                shared = self._clients[id(client)] = _SharedClient(client, None)
            shared.users += 1
            return unbound

    async def release(self, client: Any, models: Iterable[type]) -> None:
        """Undo one :meth:`bind`: the classes' bindings lose a user, and so does the client, which is closed when
        it has none left."""
        close = False
        with self._lock:
            for model in dict.fromkeys(models):
                bound = self._bindings.get(model)
                if bound is not None and bound.client is client and bound.users > 0:
                    bound.users -= 1
                    if bound.users == 0:
                        del self._bindings[model]
            shared = self._clients.get(id(client))
            if shared is not None and shared.client is client:
                shared.users -= 1
                if shared.users <= 0:
                    close = True
                    del self._clients[id(client)]
                    if shared.key is not None and self._by_settings.get(shared.key) is shared:
                        del self._by_settings[shared.key]
        if close:
            await client.close()

    def users(self, client: Any) -> int:
        """How many running contexts use *client*."""
        with self._lock:
            shared = self._clients.get(id(client))
            return shared.users if shared is not None and shared.client is client else 0

    def bound_database(self, model: type) -> str | None:
        """The database *model* is bound to by a running context, or ``None``."""
        with self._lock:
            bound = self._bindings.get(model)
            return bound.database if bound is not None and bound.users > 0 else None


BINDINGS = DocumentBindings()
"""The process's document bindings and shared clients."""


# ---------------------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------------------


def _is_document(candidate: object) -> bool:
    from beanie import Document

    return (
        isinstance(candidate, type)
        and issubclass(candidate, Document)
        and candidate is not Document
        and not getattr(candidate, "__abstract_document__", False)
        and candidate.__module__ != "pyfly.data.document.mongodb.document"
    )


def _linked(annotation: Any, found: list[type]) -> None:
    """Collect the document classes a field annotation links to (``Link[X]``, ``BackLink[X]``, in lists and
    optionals)."""
    from beanie import BackLink, Link

    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin in (Link, BackLink):
        for argument in arguments:
            if _is_document(argument):
                found.append(argument)
        return
    for argument in arguments:
        _linked(argument, found)


def with_linked_documents(models: Iterable[type]) -> list[type]:
    """*models* and, recursively, the documents their ``Link``/``BackLink`` fields name."""
    ordered: list[type] = []
    pending = list(models)
    while pending:
        model = pending.pop(0)
        if model in ordered:
            continue
        ordered.append(model)
        fields = getattr(model, "model_fields", {})
        for info in fields.values():
            linked: list[type] = []
            _linked(info.annotation, linked)
            pending.extend(linked)
    return ordered


def configured_documents(names: Iterable[str]) -> list[type]:
    """The document classes *names* designate: a class by its dotted name, or every document class of a module,
    and of a package's modules. A name that imports nothing raises ``ValueError``."""
    found: list[type] = []
    for name in names:
        try:
            module: ModuleType | None = importlib.import_module(name)
        except ImportError:
            module = None
        if module is not None:
            found.extend(_module_documents(module))
            path = getattr(module, "__path__", None)
            if path is not None:
                for info in pkgutil.walk_packages(path, prefix=f"{module.__name__}."):
                    found.extend(_module_documents(importlib.import_module(info.name)))
            continue
        module_name, _, attribute = name.rpartition(".")
        try:
            candidate = getattr(importlib.import_module(module_name), attribute) if module_name else None
        except (ImportError, AttributeError):
            candidate = None
        if not _is_document(candidate):
            raise ValueError(
                f"pyfly.data.document.models names {name!r}, which is neither a Beanie document class nor a module"
            )
        found.append(candidate)  # type: ignore[arg-type]
    return found


def _module_documents(module: ModuleType) -> list[type]:
    return [
        member for _name, member in inspect.getmembers(module, _is_document) if member.__module__ == module.__name__
    ]


# ---------------------------------------------------------------------------------------------------------
# The lifecycle bean
# ---------------------------------------------------------------------------------------------------------


class BeanieInitializer:
    """Lifecycle bean that binds the application's document classes to its database when the context starts
    (see the module documentation), and gives the binding and the client back when the context disposes its
    resources."""

    phase = DOCUMENT_SCHEMA_PHASE

    def __init__(
        self,
        motor_client: pymongo.AsyncMongoClient[Any],
        config: Config,
        container: Container,
        *,
        bindings: DocumentBindings | None = None,
    ) -> None:
        self._motor_client = motor_client
        self._config = config
        self._container = container
        self._bindings = bindings or BINDINGS
        self._bound: list[type] = []
        self._holds = False  # whether this initializer counts as a user of its client (between start and dispose)

    @property
    def documents(self) -> list[type]:
        """The document classes this initializer bound (empty before :meth:`start`)."""
        return list(self._bound)

    def discover(self) -> list[type]:
        """The document classes to bind (see the module documentation), linked documents included."""
        from pyfly.config.properties.mongodb import DocumentProperties
        from pyfly.data.document.mongodb.repository import MongoRepository

        found: list[type] = []
        for cls in list(self._container._registrations):
            if isinstance(cls, type) and issubclass(cls, MongoRepository) and cls is not MongoRepository:
                entity_type = getattr(cls, "_entity_type", None)
                if _is_document(entity_type):
                    found.append(entity_type)  # type: ignore[arg-type]
        for cls in list(self._container._registrations):
            if _is_document(cls):
                found.append(cls)
        found.extend(configured_documents(DocumentProperties.from_config(self._config).models))
        return with_linked_documents(found)

    async def start(self) -> None:
        """Bind the discovered document classes (a no-op for those this client and database have already)."""
        from pyfly.config.properties.mongodb import DocumentProperties

        if self._holds:
            return
        database = DocumentProperties.from_config(self._config).database
        documents = self.discover()
        unbound = self._bindings.bind(self._motor_client, database, documents)
        self._bound = documents
        self._holds = True
        if not unbound:
            return
        from beanie import init_beanie

        try:
            # All of them: Beanie resolves the links between the classes it initializes together.
            await init_beanie(database=self._motor_client[database], document_models=documents)
        except BaseException:
            self._bound, self._holds = [], False
            await self._bindings.release(self._motor_client, documents)
            raise
        logger.info("beanie_initialized", extra={"database": database, "documents": [d.__name__ for d in documents]})

    async def stop(self) -> None:
        """Nothing: the binding and the client are given back by :meth:`dispose_all`, once every bean stopped."""

    async def dispose_all(self) -> None:
        """Give the binding and the client back (the client is closed when no running context uses it)."""
        if not self._holds:
            return
        documents, self._bound, self._holds = self._bound, [], False
        await self._bindings.release(self._motor_client, documents)
