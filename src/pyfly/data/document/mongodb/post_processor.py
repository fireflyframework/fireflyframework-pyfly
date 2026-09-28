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
"""BeanPostProcessor that wires query methods onto MongoRepository beans."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Collection
from typing import Any

from pyfly.data.document.mongodb.properties import document_properties
from pyfly.data.document.mongodb.query import MongoAnnotatedQuery, MongoQueryExecutor
from pyfly.data.document.mongodb.query_compiler import MongoDerivedQuery, MongoQueryMethodCompiler
from pyfly.data.document.mongodb.repository import MongoRepository, deletes_in_one_command, repository_operation
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.post_processor import BaseRepositoryPostProcessor, QueryMethod, check_arguments, describe_method
from pyfly.data.query_parser import ElementKind, InvalidQueryMethodError, ResultKind, is_special_parameter
from pyfly.data.transaction.registry import TransactionManagerRegistry

logger = logging.getLogger(__name__)

_LEGACY_HOOKS = ("_compile_derived", "_wrap_derived_method")
"""The hooks a derived method is built with by the base post-processor's ``_implement_derived``."""


class MongoRepositoryBeanPostProcessor(BaseRepositoryPostProcessor):
    """Replaces stub methods on :class:`MongoRepository` subclasses with real query implementations.

    For each method decorated with ``@query``, compiles the annotated JSON filter or aggregation pipeline
    (:mod:`~pyfly.data.document.mongodb.query`). For each derived query stub (``find_by_``, ``count_by_``,
    ``exists_by_``, ``delete_by_``), parses the name against the document's fields with the shared PartTree
    parser (so a name that names no field fails when the repository is built, instead of matching nothing, or
    deleting everything) and compiles it (:mod:`~pyfly.data.document.mongodb.query_compiler`).

    Every compiled method is a repository operation like the inherited ones: it joins the current unit of
    work or runs in an auto unit (a read unit for ``find_by_``/``count_by_``/``exists_by_`` and filter or
    aggregation queries that write nothing, a write unit for ``delete_by_``, and a write unit without a
    transaction for a pipeline with an ``$out`` or ``$merge`` stage, one command that MongoDB refuses inside a
    transaction), passes the unit's session to the driver, and raises the kernel's translated persistence
    exceptions. With *transaction_managers*, every repository bean resolves its transaction manager from that
    registry (the application context's); a callable is asked for it when the first repository is initialized.
    """

    def __init__(
        self,
        transaction_managers: TransactionManagerRegistry
        | Callable[[], TransactionManagerRegistry | None]
        | None = None,
    ) -> None:
        super().__init__()
        self._query_compiler = MongoQueryMethodCompiler()
        self._query_executor = MongoQueryExecutor()
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
        if isinstance(bean, MongoRepository):
            managers = self._managers()
            if managers is not None:
                bean._bind_transaction_managers(managers)
        return bean

    # ------------------------------------------------------------------
    # Hook implementations
    # ------------------------------------------------------------------

    def _get_repository_type(self) -> type:
        return MongoRepository

    def _properties(self, entity: Any) -> Collection[str] | None:
        """The document's fields (``id`` included), so a derived name is parsed against them."""
        properties = document_properties(entity)
        return list(properties) if properties is not None else None

    def _implement_derived(self, bean: Any, method: QueryMethod) -> Callable[..., Any]:
        """The derived query of *method* as a repository operation that binds its arguments by the stub's
        signature (its ``Pageable`` or ``Sort`` parameter pages or sorts the query).

        A subclass that overrides :meth:`_compile_derived` or :meth:`_wrap_derived_method` has its methods
        built with those hooks, as :class:`~pyfly.data.post_processor.BaseRepositoryPostProcessor` builds them."""
        cls = type(self)
        if any(getattr(cls, hook) is not getattr(MongoRepositoryBeanPostProcessor, hook) for hook in _LEGACY_HOOKS):
            return super()._implement_derived(bean, method)
        entity = bean._model
        parsed = self._parse(method, entity)
        check_arguments(method, parsed)
        return_type = None if method.return_type is inspect.Signature.empty else method.return_type
        query = self._query_compiler.compile(parsed, entity, return_type=return_type, name=method.qualified_name)
        special = _special_parameters(method, query)
        pageable_name = next((name for name, kind in special.items() if kind is Pageable), None)
        sort_name = next((name for name, kind in special.items() if kind is Sort), None)

        async def derived(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            named = method.bind(args, kwargs)
            return await query.run(
                self_arg,
                _values(method, named, special),
                pageable=named.get(pageable_name) if pageable_name else None,
                sort=named.get(sort_name) if sort_name else None,
            )

        derived.__name__ = method.name
        derived.__qualname__ = f"{method.owner.__qualname__}.{method.name}"
        derived.__doc__ = method.function.__doc__
        derived.__module__ = method.function.__module__
        if parsed.prefix == "delete_by":
            # A derived delete runs the delete event actions (user code): the guard is taken per command only.
            return repository_operation(derived, read=False, atomic=False, single=deletes_in_one_command)
        return repository_operation(derived, read=True, atomic=True)

    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        return self._query_compiler.compile(parsed, entity, return_type=return_type)

    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a compiled derived query as a repository operation on the repository it is called on."""

        async def wrapper(self_arg: Any, *args: Any) -> Any:
            if isinstance(compiled_fn, MongoDerivedQuery):
                pageable = next((arg for arg in args if isinstance(arg, Pageable)), None)
                sort = next((arg for arg in args if isinstance(arg, Sort)), None)
                values = [arg for arg in args if not isinstance(arg, (Pageable, Sort))]
                return await compiled_fn.run(self_arg, values, pageable=pageable, sort=sort)
            return await compiled_fn(self_arg._model, *args)

        read = not isinstance(compiled_fn, MongoDerivedQuery) or compiled_fn.parsed.prefix != "delete_by"
        return repository_operation(wrapper, read=read, atomic=read)

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Compile a ``@query`` method into a repository operation that binds its arguments by the stub's
        signature, by position or by keyword."""
        if not hasattr(attr, "__pyfly_query__"):
            return False
        method = describe_method(cls, attr_name, attr, entity, resolve=False)
        compiled = self._query_executor.compile_query_method(attr, entity)

        async def queried(self_arg: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(compiled, MongoAnnotatedQuery):
                return await compiled.run(self_arg, **method.bind(args, kwargs))
            return await compiled(self_arg._model, **method.bind(args, kwargs))

        queried.__name__ = attr_name
        queried.__qualname__ = f"{cls.__qualname__}.{attr_name}"
        queried.__doc__ = method.function.__doc__
        queried.__module__ = method.function.__module__
        reads = compiled.reads if isinstance(compiled, MongoAnnotatedQuery) else True
        # A pipeline that writes ($out, $merge) is one command: outside a transaction it runs without one.
        operation = repository_operation(queried, read=reads, atomic=True, single=not reads)
        setattr(bean, attr_name, operation.__get__(bean, cls))
        return True


def _special_parameters(method: QueryMethod, query: MongoDerivedQuery) -> dict[str, type]:
    """The stub's ``Pageable`` and ``Sort`` parameters by name, checked against what the query returns."""
    special: dict[str, type] = {}
    for parameter in method.parameters:
        if is_special_parameter(parameter.annotation):
            special[parameter.name] = Pageable if _mentions(parameter.annotation, Pageable) else Sort
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
            f"{method.qualified_name}: only a find_by method returning several documents takes a Pageable or a Sort"
        )
    if kinds and shape.element is ElementKind.PROJECTION and Pageable in kinds and shape.kind is not ResultKind.LIST:
        raise InvalidQueryMethodError(f"{method.qualified_name}: a projection is returned as a list")
    return special


def _values(method: QueryMethod, named: dict[str, Any], special: dict[str, type]) -> list[Any]:
    """The query values of a call bound to *method*'s signature, in parameter order."""
    values: list[Any] = []
    for parameter in method.parameters:
        if parameter.name in special or parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            values.extend(named.get(parameter.name, ()))
        else:
            values.append(named[parameter.name])
    return values


def _mentions(annotation: Any, candidate: type) -> bool:
    if isinstance(annotation, type):
        return issubclass(annotation, candidate)
    return any(_mentions(member, candidate) for member in getattr(annotation, "__args__", ()))
