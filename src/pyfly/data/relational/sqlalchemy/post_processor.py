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

from collections.abc import Callable
from typing import Any

from pyfly.data.post_processor import BaseRepositoryPostProcessor
from pyfly.data.relational.sqlalchemy.query import QueryExecutor
from pyfly.data.relational.sqlalchemy.query_compiler import QueryMethodCompiler
from pyfly.data.relational.sqlalchemy.repository import Repository, is_read_method, repository_operation
from pyfly.data.transaction.registry import TransactionManagerRegistry

_READ_FLAG = "__pyfly_read_operation__"


class RepositoryBeanPostProcessor(BaseRepositoryPostProcessor):
    """Replaces stub methods on :class:`Repository` subclasses with real query implementations.

    For each method decorated with ``@query``, compiles the annotated SQL/JPQL
    into an executable async callable.  For each derived query stub (methods
    starting with ``find_by_``, ``count_by_``, ``exists_by_``, or
    ``delete_by_``), parses the method name and compiles a corresponding
    SQLAlchemy query.

    Every compiled method is a repository operation like the inherited ones: it joins the current unit of
    work or runs in an auto unit (a read unit for ``find_by_``/``count_by_``/``exists_by_`` and ``SELECT``
    queries, a write unit for the rest). With *transaction_managers*, every repository bean resolves its
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

    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        compiled = self._query_compiler.compile(parsed, entity, return_type=return_type)
        setattr(compiled, _READ_FLAG, parsed.prefix != "delete_by")
        return compiled

    def _wrap_derived_method(self, compiled_fn: Any) -> Any:
        """Wrap a derived-query-compiled function as a repository operation on ``bean._session``."""

        async def wrapper(self_arg: Any, *args: Any) -> Any:
            return await compiled_fn(self_arg._session, *args)

        return repository_operation(wrapper, read=bool(getattr(compiled_fn, _READ_FLAG, False)), atomic=True)

    def _process_query_decorated(self, bean: Any, cls: type, attr_name: str, attr: Any, entity: Any) -> bool:
        """Process ``@query``-decorated methods."""
        if hasattr(attr, "__pyfly_query__"):
            compiled_fn = self._query_executor.compile_query_method(attr, entity)
            read = is_read_method(attr_name) or str(attr.__pyfly_query__).lstrip().upper().startswith("SELECT")
            wrapper = self._wrap_query_method(compiled_fn, read=read)
            setattr(bean, attr_name, wrapper.__get__(bean, cls))
            return True
        return False

    # ------------------------------------------------------------------
    # Wrapper factories
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_query_method(compiled_fn: Any, *, read: bool = False) -> Any:
        """Wrap a ``@query``-compiled function as a repository operation on ``bean._session``."""

        async def wrapper(self_arg: Any, **kwargs: Any) -> Any:
            return await compiled_fn(self_arg._session, **kwargs)

        return repository_operation(wrapper, read=read, atomic=True)
