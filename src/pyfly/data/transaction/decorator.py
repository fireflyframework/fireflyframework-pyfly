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
"""``@transactional``: declarative transactions for every backend (Spring's ``@Transactional``).

It works bare (``@transactional``) or parametrized (``@transactional(propagation=...)``), on an
``async def`` method or function, or on a class (every public ``async def`` defined on it; a method's own
settings override the class's). At decoration time it raises ``TypeError`` for a sync function and for an
async generator: a transaction cannot safely span iteration, and
:class:`~pyfly.data.transaction.template.TransactionTemplate` is the tool for that.

The transaction manager is resolved per call, in this order:

1. ``manager=`` (a :class:`~pyfly.data.transaction.manager.TransactionManager` or a datasource name) or
   ``datasource="name"``, on the method or on a class-level ``@transactional``;
2. the legacy attributes, kept for compatibility: ``self._session_factory`` (an ``async_sessionmaker``,
   mapped to its registry datasource) and ``self._motor_client``. A service that exposes both raises
   :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` unless a datasource is named;
3. the default datasource of the running application context (``primary``). A service with no factory
   attribute works, and so does a plain function.

``Propagation``, ``Isolation``, ``read_only``, ``timeout`` (seconds, new units only), ``rollback_for``
and ``no_rollback_for`` (additive rules: any ``Exception`` rolls back unless a more specific
``no_rollback_for`` class matches) behave as in Spring on every backend; see
:mod:`pyfly.data.transaction.template`.

When ``@transactional`` decorates a function already wrapped by ``@retry``, it moves the retry outside
the transaction (Spring orders its retry advice outside the transaction interceptor), so every attempt
runs in a fresh unit whichever order the decorators are written in.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine
from typing import Any

from pyfly.data.transaction.definition import Isolation, Propagation, TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionManager
from pyfly.data.transaction.registry import find_manager_for_resource, resolve_manager
from pyfly.data.transaction.template import execute_in_transaction

_LEGACY_DOCUMENT = object()
"""Resolution result: run the pre-unit-of-work MongoDB runner (no Mongo manager is registered yet)."""


def transactional(
    func: Any = None,
    /,
    *,
    propagation: Propagation | None = None,
    isolation: Isolation | None = None,
    read_only: bool | None = None,
    timeout: float | None = None,
    rollback_for: tuple[type[BaseException], ...] | None = None,
    no_rollback_for: tuple[type[BaseException], ...] | None = None,
    datasource: str | None = None,
    manager: TransactionManager | str | None = None,
    name: str | None = None,
) -> Any:
    """Run the decorated coroutine function (or every public coroutine method of a decorated class) inside
    a transactional boundary.

    Args:
        propagation: How the call relates to a unit already bound for its datasource (default ``REQUIRED``).
        isolation: The isolation level of a new unit, validated per dialect (default ``DEFAULT``).
        read_only: Route a new unit to the datasource's replica, refuse ORM writes, add the dialect hint.
        timeout: Seconds a new unit's body may run; on expiry it rolls back (``TransactionTimedOutError``).
        rollback_for: Extra exception classes that roll back (any ``Exception`` already does).
        no_rollback_for: Exception classes that commit, unless a more specific rollback rule matches.
        datasource: The datasource whose transaction manager runs the call.
        manager: A transaction manager (or a datasource name) to use instead of resolving one.
        name: A label for the unit in logs and errors.
    """
    options = {
        key: value
        for key, value in (
            ("propagation", propagation),
            ("isolation", isolation),
            ("read_only", read_only),
            ("timeout", timeout),
            ("rollback_for", rollback_for),
            ("no_rollback_for", no_rollback_for),
            ("datasource", datasource),
            ("manager", manager),
            ("name", name),
        )
        if value is not None
    }

    def decorate(target: Any) -> Any:
        if isinstance(target, type):
            return _decorate_class(target, options)
        return _decorate_function(target, options)

    return decorate(func) if func is not None else decorate


def _definition(options: dict[str, Any]) -> TransactionDefinition:
    return TransactionDefinition(
        propagation=options.get("propagation", Propagation.REQUIRED),
        isolation=options.get("isolation", Isolation.DEFAULT),
        read_only=bool(options.get("read_only", False)),
        timeout=options.get("timeout"),
        rollback_for=tuple(options.get("rollback_for", ())),
        no_rollback_for=tuple(options.get("no_rollback_for", ())),
        datasource=options.get("datasource"),
        name=options.get("name"),
    )


def _decorate_class(cls: type, options: dict[str, Any]) -> type:
    """Decorate every public coroutine method *cls* defines; a method's own settings win over the class's."""
    for attr_name, attr in list(vars(cls).items()):
        if attr_name.startswith("_"):
            continue
        wrapper_type: type[staticmethod[Any, Any]] | type[classmethod[Any, Any, Any]] | None = None
        function = attr
        if isinstance(attr, (staticmethod, classmethod)):
            wrapper_type = type(attr)
            function = attr.__func__
        original = getattr(function, "__pyfly_transaction_original__", None)
        if original is not None:
            merged = {**options, **getattr(function, "__pyfly_transaction_options__", {})}
            decorated = _decorate_function(original, merged)
        elif inspect.iscoroutinefunction(function) and not inspect.isasyncgenfunction(function):
            decorated = _decorate_function(function, options)
        else:
            continue
        setattr(cls, attr_name, wrapper_type(decorated) if wrapper_type is not None else decorated)
    cls.__pyfly_transactional_options__ = dict(options)  # type: ignore[attr-defined]
    return cls


def _decorate_function(function: Any, options: dict[str, Any]) -> Any:
    if isinstance(function, (staticmethod, classmethod)):
        return type(function)(_decorate_function(function.__func__, options))
    retry_decorator = getattr(function, "__pyfly_retry__", None)
    inner = getattr(function, "__wrapped__", None)
    if retry_decorator is not None and inner is not None:
        # @transactional over @retry: put the retry outside, so each attempt gets a fresh unit.
        hoisted = retry_decorator(_decorate_function(inner, options))
        hoisted.__pyfly_transaction_original__ = function
        hoisted.__pyfly_transaction_options__ = dict(options)
        return hoisted
    qualname = getattr(function, "__qualname__", repr(function))
    if inspect.isasyncgenfunction(function):
        raise TypeError(
            f"@transactional cannot decorate the async generator {qualname}: a transaction cannot safely span "
            "iteration. Collect the items inside a transactional method, or open "
            "`async with TransactionTemplate(...).transaction():` around the loop."
        )
    if not inspect.iscoroutinefunction(function):
        raise TypeError(
            f"@transactional needs an `async def` function, and {qualname} is not a coroutine function. "
            "Make it async (the data layer is async end to end)."
        )
    definition = _definition(options)
    target = options.get("manager")
    coroutine: Callable[..., Coroutine[Any, Any, Any]] = function

    @functools.wraps(coroutine)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        manager = _resolve(qualname, definition, target, args)
        if manager is _LEGACY_DOCUMENT:
            from pyfly.data.document.mongodb.transactional import run_mongo_transaction

            return await run_mongo_transaction(
                coroutine, args, kwargs, rollback_for=(Exception,), no_rollback_for=definition.no_rollback_for
            )
        assert isinstance(manager, TransactionManager)
        return await execute_in_transaction(manager, definition, coroutine, args, kwargs)

    wrapper.__pyfly_transactional__ = True  # type: ignore[attr-defined]
    wrapper.__pyfly_propagation__ = definition.propagation  # type: ignore[attr-defined]
    wrapper.__pyfly_isolation__ = definition.isolation  # type: ignore[attr-defined]
    wrapper.__pyfly_transaction_definition__ = definition  # type: ignore[attr-defined]
    wrapper.__pyfly_transaction_original__ = function  # type: ignore[attr-defined]
    wrapper.__pyfly_transaction_options__ = dict(options)  # type: ignore[attr-defined]
    return wrapper


def _resolve(qualname: str, definition: TransactionDefinition, target: object, args: tuple[Any, ...]) -> object:
    """The transaction manager of one call (see the module documentation for the order)."""
    if target is not None:
        return resolve_manager(target)
    if definition.datasource is not None:
        return resolve_manager(definition.datasource)
    holder = args[0] if args else None
    factory = getattr(holder, "_session_factory", None) if holder is not None else None
    client = getattr(holder, "_motor_client", None) if holder is not None else None
    if factory is not None and client is not None:
        raise IllegalTransactionStateError(
            f"{qualname}: the service exposes both a relational '_session_factory' and a document "
            "'_motor_client', so @transactional cannot tell which transaction to run (one arm would commit "
            "while the other rolls back). Name one: @transactional(datasource='...') or "
            "@transactional(manager=...)."
        )
    if factory is not None:
        manager = find_manager_for_resource(factory)
        if manager is None:
            raise IllegalTransactionStateError(
                f"{qualname}: '_session_factory' is a {type(factory).__name__}, not a session factory a "
                "transaction manager serves."
            )
        return manager
    if client is not None:
        manager = find_manager_for_resource(client)
        return manager if manager is not None else _LEGACY_DOCUMENT
    return resolve_manager(None)


def is_transactional(function: object) -> bool:
    """Whether *function* was decorated with ``@transactional``."""
    return bool(getattr(function, "__pyfly_transactional__", False))


__all__ = ["is_transactional", "transactional"]
