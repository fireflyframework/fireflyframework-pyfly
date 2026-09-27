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
"""BeanPostProcessor that wires query methods onto Repository beans."""

from __future__ import annotations

import inspect
import threading
import weakref
from collections.abc import Callable, Collection
from typing import Any

from pyfly.data.pageable import Pageable, Sort
from pyfly.data.post_processor import BaseRepositoryPostProcessor, QueryMethod, check_arguments, describe_method
from pyfly.data.query_parser import ElementKind, InvalidQueryMethodError, ParsedQuery, ResultKind, is_special_parameter
from pyfly.data.relational.sqlalchemy.query import QueryExecutor
from pyfly.data.relational.sqlalchemy.query_compiler import DerivedQuery, QueryMethodCompiler, derived_properties
from pyfly.data.relational.sqlalchemy.repository import Repository, repository_operation
from pyfly.data.transaction.registry import TransactionManagerRegistry

_READ_FLAG = "__pyfly_read_operation__"

_COMPILED: weakref.WeakKeyDictionary[type, dict[tuple[type, str], DerivedQuery]] = weakref.WeakKeyDictionary()
"""The derived queries compiled per repository class, by entity and method name: a transient or request-scoped
repository compiles its methods once, not once per instance."""

_COMPILED_LOCK = threading.Lock()


class RepositoryBeanPostProcessor(BaseRepositoryPostProcessor):
    """Replaces stub methods on :class:`Repository` subclasses with real query implementations.

    For each method decorated with ``@query``, compiles the annotated SQL/JPQL
    into an executable async callable.  For each derived query stub (methods
    starting with ``find_by_``, ``count_by_``, ``exists_by_``, or
    ``delete_by_``), parses the method name against the entity's properties and compiles a corresponding
    SQLAlchemy query (:mod:`~pyfly.data.relational.sqlalchemy.query_compiler`), whose statement is built once.
    A method that does not match its entity fails here, when the repository is built.

    Every compiled method is a repository operation like the inherited ones: it joins the current unit of
    work or runs in an auto unit (a read unit for ``find_by_``/``count_by_``/``exists_by_`` and ``SELECT``
    queries, a write unit for the rest), and raises the kernel's translated persistence exceptions. It takes
    its arguments by position or by keyword. With *transaction_managers*, every repository bean resolves its
    transaction manager from that registry (the application context's); a callable is asked for it when
    the first repository is initialized, once every bean of the context is registered.
    """

    def __init__(
        self,
        transaction_managers: TransactionManagerRegistry
        | Callable[[], TransactionManagerRegistry | None]
        | None = None,
    ) -> None:
        super().__init__()
        self._query_executor = QueryExecutor()
        self._query_compiler = QueryMethodCompiler()
        self._transaction_managers = transaction_managers

    def _managers(self) -> TransactionManagerRegistry | None:
        managers = self._transaction_managers
        if managers is None or isinstance(managers, TransactionManagerRegistry):
            return managers
        resolved = managers()
        if resolved is not None:
            self._transaction_managers = resolved
        return resolved

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Compile the query methods, and bind the transaction managers of the context."""
        bean = super().after_init(bean, bean_name)
        if isinstance(bean, Repository):
            managers = self._managers()
            if managers is not None:
                bean._bind_transaction_managers(managers)
        return bean

    # ------------------------------------------------------------------
    # Hook implementations
    # ------------------------------------------------------------------

    def _get_repository_type(self) -> type:
        return Repository

    def _properties(self, entity: Any) -> Collection[str]:
        """The entity's columns, synonyms, hybrids and relationships to one entity."""
        return derived_properties(entity)

    def _implement_derived(self, bean: Any, method: QueryMethod) -> Callable[..., Any]:
        """The derived query of *method*, compiled once per repository class and entity, as a repository
        operation that binds its arguments by the stub's signature (its ``Pageable`` or ``Sort`` parameter
        pages or sorts the query)."""
        entity = bean._model
        parsed = self._parse(method, entity)
        check_arguments(method, parsed)
        query = self._derived_query(bean, method, parsed)
        special = _special_parameters(method, query)
        pageable_name = next((name for name, kind in special.items() if kind is Pageable), None)
        sort_name = next((name for name, kind in special.items() if kind is Sort), None)
        # Every parameter binds a value, and each can come by position: the common call passes them so, in order.
        positional = not special and all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for parameter in method.parameters
        )
        arity = len(method.parameters)

        async def derived(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            if positional and not kwargs and len(args) == arity:
                return await query.run(self_arg, self_arg._session, args)
            named = method.bind(args, kwargs)
            return await query.run(
                self_arg,
                self_arg._session,
                _values(method, named, special),
                pageable=named.get(pageable_name) if pageable_name else None,
                sort=named.get(sort_name) if sort_name else None,
            )

        derived.__name__ = method.name
        derived.__qualname__ = f"{method.owner.__qualname__}.{method.name}"
        derived.__doc__ = method.function.__doc__
        derived.__module__ = method.function.__module__
        return repository_operation(derived, read=parsed.prefix != "delete_by", atomic=True)

    def _derived_query(self, bean: Any, method: QueryMethod, parsed: ParsedQuery) -> DerivedQuery:
        cls = type(bean)
        key = (bean._model, method.name)
        with _COMPILED_LOCK:
            compiled = _COMPILED.setdefault(cls, {})
            query = compiled.get(key)
        if query is None:
            return_type = None if method.return_type is inspect.Signature.empty else method.return_type
            query = self._query_compiler.compile(
                parsed, bean._model, return_type=return_type, repository=bean, name=method.qualified_name
            )
            with _COMPILED_LOCK:
                query = _COMPILED.setdefault(cls, {}).setdefault(key, query)
        return query

    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        compiled = self._query_compiler.compile(parsed, entity, return_type=return_type, repository=bean)
        setattr(compiled, _READ_FLAG, parsed.prefix != "delete_by")
        return compiled

    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a derived-query-compiled function as a repository operation on ``bean._session`` (for the
        repository the call is made on: its soft delete, read criteria and paging)."""

        async def wrapper(self_arg: Any, *args: Any) -> Any:
            if isinstance(compiled_fn, DerivedQuery):
                pageable = next((arg for arg in args if isinstance(arg, Pageable)), None)
                sort = next((arg for arg in args if isinstance(arg, Sort)), None)
                values = [arg for arg in args if not isinstance(arg, (Pageable, Sort))]
                return await compiled_fn.run(self_arg, self_arg._session, values, pageable=pageable, sort=sort)
            return await compiled_fn(self_arg._session, *args)

        return repository_operation(wrapper, read=bool(getattr(compiled_fn, _READ_FLAG, False)), atomic=True)

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Compile a ``@query`` method (:mod:`~pyfly.data.relational.sqlalchemy.query`) into a repository
        operation that binds its arguments by the stub's signature, by position or by keyword: a read for a
        query, a write for a ``@modifying`` statement."""
        if not hasattr(attr, "__pyfly_query__"):
            return False
        method = describe_method(cls, attr_name, attr, entity, resolve=False)  # the query reads its annotations
        compiled = self._query_executor.compile_query_method(attr, entity, name=method.qualified_name)

        async def queried(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            return await compiled(self_arg._session, **method.bind(args, kwargs))

        queried.__name__ = attr_name
        queried.__qualname__ = f"{cls.__qualname__}.{attr_name}"
        queried.__doc__ = method.function.__doc__
        queried.__module__ = method.function.__module__
        read = not getattr(compiled, "is_modifying", False)
        setattr(bean, attr_name, repository_operation(queried, read=read, atomic=True).__get__(bean, cls))
        return True

    # ------------------------------------------------------------------
    # Wrapper factories
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_query_method(compiled_fn: Any, *, read: bool = False) -> Any:
        """Wrap a ``@query``-compiled function as a repository operation on ``bean._session`` (keyword
        arguments only; the post-processor binds positional ones by the method's signature first)."""

        async def wrapper(self_arg: Any, **kwargs: Any) -> Any:
            return await compiled_fn(self_arg._session, **kwargs)

        return repository_operation(wrapper, read=read, atomic=True)


def _special_parameters(method: QueryMethod, query: DerivedQuery) -> dict[str, type]:
    """The stub's ``Pageable`` and ``Sort`` parameters by name, checked against what the query returns: a
    ``Page`` or ``Slice`` needs a ``Pageable``, and a single result, a count, an existence check or a delete takes
    neither."""
    special: dict[str, type] = {}
    for parameter in method.parameters:
        if is_special_parameter(parameter.annotation):
            special[parameter.name] = _special_type(parameter.annotation)
    kinds = list(special.values())
    shape = query.shape
    prefix = query.parsed.prefix
    if len(kinds) != len(set(kinds)) or (Pageable in kinds and Sort in kinds):
        raise InvalidQueryMethodError(
            f"{method.qualified_name}: it takes one Pageable or one Sort (a Pageable carries its own sort)"
        )
    if shape.kind in (ResultKind.PAGE, ResultKind.SLICE) and Pageable not in kinds:
        raise InvalidQueryMethodError(f"{method.qualified_name}: a Page or Slice result needs a Pageable parameter")
    if kinds and (prefix != "find_by" or shape.kind is ResultKind.ONE):
        raise InvalidQueryMethodError(
            f"{method.qualified_name}: only a find_by method returning several entities takes a Pageable or a Sort"
        )
    if kinds and shape.element is ElementKind.PROJECTION:
        raise InvalidQueryMethodError(f"{method.qualified_name}: a projection takes no Pageable or Sort")
    return special


def _values(method: QueryMethod, named: dict[str, Any], special: dict[str, type]) -> list[Any]:
    """The query values of a call bound to *method*'s signature, in parameter order (``*args`` expanded; the
    special parameters and ``**kwargs`` left out)."""
    values: list[Any] = []
    for parameter in method.parameters:
        if parameter.name in special or parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            values.extend(named.get(parameter.name, ()))
        else:
            values.append(named[parameter.name])
    return values


def _special_type(annotation: Any) -> type:
    for candidate in (Pageable, Sort):
        if is_special_parameter(annotation) and _mentions(annotation, candidate):
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover — only special parameters are asked


def _mentions(annotation: Any, candidate: type) -> bool:
    if isinstance(annotation, type):
        return issubclass(annotation, candidate)
    return any(_mentions(member, candidate) for member in getattr(annotation, "__args__", ()))
